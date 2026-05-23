"""
workflow/task_manager.py

Durable Compliance Task Manager
════════════════════════════════
Manages the full compliance ticket lifecycle using a local durable state machine
with persistent snapshots, SLA-aware timers, smart escalation routing, and
integrations with ServiceNow, Microsoft Teams, and Email.

Lifecycle
─────────
  DRAFT ──► APPROVED ──► ASSIGNED ──► IN_PROGRESS ──► COMPLETED ──► VALIDATING ──► CLOSED
              │               │              │
           (reject)        (unassign)    (blocked)
              ▼               ▼              ▼
            DRAFT           DRAFT       IN_PROGRESS (retry)

SLA Windows
───────────
  High Risk   →  7 days
  Medium Risk → 14 days
  Low Risk    → 30 days

Escalation Ladder
─────────────────
  80 % SLA elapsed  → warn  Unit Head         (Teams + Email)
  100% SLA elapsed  → alert Compliance Officer (Teams + Email)
  Breach + High Risk → CCO escalation          (Teams + Email + SMS stub)

Smart Noise Reduction
──────────────────────
  • Deduplication window  : identical alert suppressed for N minutes
  • Exponential back-off  : repeated escalation alerts grow quieter
  • Digest mode           : batches low-severity alerts every 30 min
  • Cooldown registry     : per-(task, level) timestamps stored in AlertStore
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
import os
import asyncio
import traceback
try:
    from azure.cosmos.aio import CosmosClient as AsyncCosmosClient
    from azure.cosmos.exceptions import CosmosResourceExistsError
    try:
        from azure.cosmos.exceptions import CosmosHttpResponseError
    except Exception:
        CosmosHttpResponseError = Exception
    _COSMOS_AIO_AVAILABLE = True
except Exception:
    AsyncCosmosClient = None
    CosmosResourceExistsError = Exception
    CosmosHttpResponseError = Exception
    _COSMOS_AIO_AVAILABLE = False
from typing import Any, Callable, Coroutine

# ─────────────────────────────────────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s – %(message)s",
)
logger = logging.getLogger("task_manager")


# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────
SLA_DAYS: dict[str, int] = {
    "High":   7,
    "Medium": 14,
    "Low":    30,
}

ESCALATION_WARN_PCT    = 0.80    # 80 %  → Unit Head warning
ESCALATION_BREACH_PCT  = 1.00   # 100 % → Compliance Officer alert
SLA_POLL_INTERVAL_SEC  = 60     # how often the timer loop ticks (production: 60 s)

# Noise-reduction defaults
DEDUP_WINDOW_MINUTES   = 30     # suppress identical alert for 30 min
DIGEST_INTERVAL_MIN    = 30     # batch low-severity alerts every 30 min
MAX_BACKOFF_MINUTES    = 480    # cap exponential back-off at 8 hours

# Persistence directory (swap for Redis / Azure Table Storage in prod)
STATE_DIR = Path(".task_state")


# ─────────────────────────────────────────────────────────────────────────────
# Enumerations
# ─────────────────────────────────────────────────────────────────────────────

class TicketStatus(str, Enum):
    DRAFT       = "DRAFT"
    APPROVED    = "APPROVED"
    ASSIGNED    = "ASSIGNED"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED   = "COMPLETED"
    VALIDATING  = "VALIDATING"
    CLOSED      = "CLOSED"
    REJECTED    = "REJECTED"
    BREACHED    = "BREACHED"      # SLA breached, pending escalation resolution


class EscalationLevel(str, Enum):
    WARN    = "WARN"       # 80 % — Unit Head
    ALERT   = "ALERT"      # 100 % — Compliance Officer
    BREACH  = "BREACH"     # High Risk breach — CCO


class AlertChannel(str, Enum):
    TEAMS = "TEAMS"
    EMAIL = "EMAIL"
    SMS   = "SMS"


class TransitionEvent(str, Enum):
    SUBMIT    = "SUBMIT"
    APPROVE   = "APPROVE"
    REJECT    = "REJECT"
    ASSIGN    = "ASSIGN"
    UNASSIGN  = "UNASSIGN"
    START     = "START"
    BLOCK     = "BLOCK"
    UNBLOCK   = "UNBLOCK"
    COMPLETE  = "COMPLETE"
    VALIDATE  = "VALIDATE"
    CLOSE     = "CLOSE"
    BREACH    = "BREACH"


# ─────────────────────────────────────────────────────────────────────────────
# State Machine – valid transitions
# ─────────────────────────────────────────────────────────────────────────────

TRANSITIONS: dict[TicketStatus, dict[TransitionEvent, TicketStatus]] = {
    TicketStatus.DRAFT: {
        TransitionEvent.APPROVE: TicketStatus.APPROVED,
        TransitionEvent.REJECT:  TicketStatus.REJECTED,
    },
    TicketStatus.APPROVED: {
        TransitionEvent.ASSIGN:  TicketStatus.ASSIGNED,
        TransitionEvent.REJECT:  TicketStatus.DRAFT,
    },
    TicketStatus.ASSIGNED: {
        TransitionEvent.START:   TicketStatus.IN_PROGRESS,
        TransitionEvent.UNASSIGN:TicketStatus.DRAFT,
    },
    TicketStatus.IN_PROGRESS: {
        TransitionEvent.COMPLETE: TicketStatus.COMPLETED,
        TransitionEvent.BLOCK:    TicketStatus.IN_PROGRESS,    # stays, dependency recorded
        TransitionEvent.BREACH:   TicketStatus.BREACHED,
    },
    TicketStatus.COMPLETED: {
        TransitionEvent.VALIDATE: TicketStatus.VALIDATING,
    },
    TicketStatus.VALIDATING: {
        TransitionEvent.CLOSE:    TicketStatus.CLOSED,
        TransitionEvent.REJECT:   TicketStatus.IN_PROGRESS,   # validation fail → rework
    },
    TicketStatus.BREACHED: {
        TransitionEvent.APPROVE:  TicketStatus.IN_PROGRESS,   # CCO re-approves
        TransitionEvent.CLOSE:    TicketStatus.CLOSED,
    },
}


# ─────────────────────────────────────────────────────────────────────────────
# Data Models
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Contact:
    name:  str
    email: str
    teams: str     # Teams user principal name / webhook URL


@dataclass
class AuditEntry:
    timestamp:   str
    event:       str
    from_status: str
    to_status:   str
    actor:       str
    note:        str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Dependency:
    task_id:      str
    snow_id:      str | None
    description:  str
    resolved:     bool = False

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class SLARecord:
    risk_level:        str
    sla_days:          int
    started_at:        str           # ISO-8601
    deadline:          str           # ISO-8601
    warn_at:           str           # ISO-8601 — 80 %
    warn_sent:         bool = False
    alert_sent:        bool = False
    breach_sent:       bool = False
    breached:          bool = False
    elapsed_pct:       float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class TicketRecord:
    ticket_id:       str
    obligation_id:   str
    task_id:         str             # from routing layer
    department:      str
    title:           str
    description:     str
    risk_level:      str             # High | Medium | Low
    status:          TicketStatus
    assigned_to:     Contact | None
    unit_head:       Contact
    compliance_officer: Contact
    cco:             Contact
    sla:             SLARecord
    snow_ticket_id:  str | None
    dependencies:    list[Dependency]
    audit_trail:     list[AuditEntry]
    created_at:      str
    updated_at:      str
    closed_at:       str | None = None
    metadata:        dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = {
            "ticket_id":         self.ticket_id,
            "obligation_id":     self.obligation_id,
            "task_id":           self.task_id,
            "department":        self.department,
            "title":             self.title,
            "description":       self.description,
            "risk_level":        self.risk_level,
            "status":            self.status.value,
            "assigned_to":       asdict(self.assigned_to) if self.assigned_to else None,
            "unit_head":         asdict(self.unit_head),
            "compliance_officer":asdict(self.compliance_officer),
            "cco":               asdict(self.cco),
            "sla":               self.sla.to_dict(),
            "snow_ticket_id":    self.snow_ticket_id,
            "dependencies":      [dep.to_dict() for dep in self.dependencies],
            "audit_trail":       [a.to_dict() for a in self.audit_trail],
            "created_at":        self.created_at,
            "updated_at":        self.updated_at,
            "closed_at":         self.closed_at,
            "metadata":          self.metadata,
        }
        return d


# ─────────────────────────────────────────────────────────────────────────────
# Persistence Layer  (swap for Redis / Azure Cosmos in production)
# ─────────────────────────────────────────────────────────────────────────────

class TicketStore:
    """JSON-file-backed durable store. One file per ticket."""

    def __init__(self, directory: Path = STATE_DIR) -> None:
        self._dir = directory
        self._dir.mkdir(parents=True, exist_ok=True)

    def _path(self, ticket_id: str) -> Path:
        return self._dir / f"{ticket_id}.json"

    def save(self, ticket: TicketRecord) -> None:
        self._path(ticket.ticket_id).write_text(
            json.dumps(ticket.to_dict(), indent=2, default=str),
            encoding="utf-8",
        )

    def load(self, ticket_id: str) -> dict[str, Any] | None:
        p = self._path(ticket_id)
        if not p.exists():
            return None
        return json.loads(p.read_text(encoding="utf-8"))

    def all_ids(self) -> list[str]:
        return [p.stem for p in self._dir.glob("*.json")]

    def delete(self, ticket_id: str) -> None:
        p = self._path(ticket_id)
        if p.exists():
            p.unlink()


from utils.helpers import retry_async


class CosmosTicketStore:
    """Async Cosmos DB-backed ticket store.

    Container schema: documents stored with `id` = ticket_id
    and full ticket dict in the document.
    """

    def __init__(self, connection_string: str, database: str = "compliancedb", container: str = "task_registry") -> None:
        if not _COSMOS_AIO_AVAILABLE:
            raise ImportError("azure-cosmos[aio] is required for CosmosTicketStore")
        self._conn = connection_string
        self._db = database
        self._container = container
        self._client = AsyncCosmosClient.from_connection_string(self._conn)
        self._database = None
        self._container_client = None

    async def init(self) -> None:
        # Create/get database & container clients
        self._database = self._client.get_database_client(self._db)
        self._container_client = self._database.get_container_client(self._container)

    @retry_async(
        max_attempts=4,
        base_delay=0.5,
        backoff_factor=2.0,
        max_delay=10.0,
        retriable_exceptions=(CosmosHttpResponseError,)
    )
    async def save_async(self, ticket: TicketRecord) -> None:
        doc = ticket.to_dict()
        doc["id"] = ticket.ticket_id
        try:
            await self._container_client.upsert_item(doc)
        except Exception:
            # Log and reraise to let caller handle
            raise

    @retry_async(
        max_attempts=3,
        base_delay=0.2,
        backoff_factor=2.0,
        max_delay=5.0,
        retriable_exceptions=(CosmosHttpResponseError,)
    )
    async def load_async(self, ticket_id: str) -> dict | None:
        try:
            item = await self._container_client.read_item(item=ticket_id, partition_key=ticket_id)
            return item
        except Exception:
            return None

    @retry_async(
        max_attempts=3,
        base_delay=0.5,
        backoff_factor=2.0,
        max_delay=10.0,
        retriable_exceptions=(CosmosHttpResponseError,)
    )
    async def all_ids_async(self) -> list[str]:
        query = "SELECT c.id FROM c"
        items = self._container_client.query_items(query=query, enable_cross_partition_query=True)
        res = [i["id"] for i in items]
        return res

    @retry_async(
        max_attempts=3,
        base_delay=0.2,
        backoff_factor=2.0,
        max_delay=5.0,
        retriable_exceptions=(CosmosHttpResponseError,)
    )
    async def delete_async(self, ticket_id: str) -> None:
        try:
            await self._container_client.delete_item(item=ticket_id, partition_key=ticket_id)
        except Exception:
            pass

    @retry_async(
        max_attempts=4,
        base_delay=0.5,
        backoff_factor=2.0,
        max_delay=10.0,
        retriable_exceptions=(CosmosHttpResponseError,)
    )
    async def query_due_slas(self, now_iso: str) -> list[dict]:
        # Find tickets with sla.deadline < now and not in terminal states
        q = "SELECT * FROM c WHERE c.sla.deadline < @now AND c.status NOT IN ('CLOSED','REJECTED','BREACHED')"
        params = [{"name": "@now", "value": now_iso}]
        items = self._container_client.query_items(query=q, parameters=params, enable_cross_partition_query=True)
        return [i async for i in items]


# ─────────────────────────────────────────────────────────────────────────────
# Smart Alert Store  (noise reduction registry)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class _AlertRecord:
    last_sent:   datetime
    send_count:  int
    suppressed:  int

    def next_allowed(self) -> datetime:
        """Exponential back-off, capped at MAX_BACKOFF_MINUTES."""
        minutes = min(DEDUP_WINDOW_MINUTES * (2 ** (self.send_count - 1)), MAX_BACKOFF_MINUTES)
        return self.last_sent + timedelta(minutes=minutes)


class AlertStore:
    """
    In-process dedup/backoff registry.
    Key = SHA-256(ticket_id + escalation_level + channel).
    """

    def __init__(self) -> None:
        self._registry: dict[str, _AlertRecord] = {}
        self._digest_queue: list[dict] = []
        self._last_digest_flush: datetime = _now_dt()

    def _key(self, ticket_id: str, level: EscalationLevel, channel: AlertChannel) -> str:
        raw = f"{ticket_id}:{level.value}:{channel.value}"
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    def should_send(
        self,
        ticket_id: str,
        level: EscalationLevel,
        channel: AlertChannel,
    ) -> bool:
        key = self._key(ticket_id, level, channel)
        rec = self._registry.get(key)
        if rec is None:
            return True
        return _now_dt() >= rec.next_allowed()

    def record_send(
        self,
        ticket_id: str,
        level: EscalationLevel,
        channel: AlertChannel,
    ) -> None:
        key = self._key(ticket_id, level, channel)
        rec = self._registry.get(key)
        if rec is None:
            self._registry[key] = _AlertRecord(
                last_sent=_now_dt(), send_count=1, suppressed=0
            )
        else:
            rec.last_sent  = _now_dt()
            rec.send_count += 1

    def record_suppressed(
        self,
        ticket_id: str,
        level: EscalationLevel,
        channel: AlertChannel,
    ) -> None:
        key = self._key(ticket_id, level, channel)
        rec = self._registry.get(key)
        if rec:
            rec.suppressed += 1

    def enqueue_digest(self, payload: dict) -> None:
        self._digest_queue.append(payload)

    def flush_digest(self) -> list[dict]:
        """Return queued digest messages if interval elapsed."""
        if _now_dt() - self._last_digest_flush >= timedelta(minutes=DIGEST_INTERVAL_MIN):
            batch = list(self._digest_queue)
            self._digest_queue.clear()
            self._last_digest_flush = _now_dt()
            return batch
        return []


# ─────────────────────────────────────────────────────────────────────────────
# ServiceNow Client  (mock — swap for pysnc / requests)
# ─────────────────────────────────────────────────────────────────────────────

class ServiceNowClient:
    """
    Minimal ServiceNow REST API wrapper.
    Mocked with realistic response shapes; replace with real HTTP calls.
    """

    def __init__(
        self,
        instance_url: str = "https://company.service-now.com",
        username:     str = "svc_compliance",
        password:     str = "CHANGEME",
    ) -> None:
        self._base = instance_url
        self._auth = (username, password)

    async def open_ticket(self, ticket: TicketRecord) -> str:
        """Create an Incident / Change Request in ServiceNow. Returns sys_id."""
        payload = {
            "short_description": ticket.title,
            "description":       ticket.description,
            "category":          "Compliance",
            "subcategory":       ticket.department,
            "priority":          self._risk_to_priority(ticket.risk_level),
            "assignment_group":  ticket.department,
            "u_obligation_id":   ticket.obligation_id,
            "u_task_id":         ticket.task_id,
            "u_sla_deadline":    ticket.sla.deadline,
        }
        # ── MOCK response ────────────────────────────────────────────────────
        snow_id = f"INC{uuid.uuid4().hex[:7].upper()}"
        logger.info("[SNOW] Opened ticket %s for task %s", snow_id, ticket.task_id)
        return snow_id

    async def update_ticket(
        self,
        snow_id: str,
        state:   str,
        comment: str = "",
    ) -> None:
        """Update work notes and state on an existing ServiceNow ticket."""
        logger.info("[SNOW] Updated %s → state=%s", snow_id, state)

    async def link_dependency(
        self,
        parent_snow_id: str,
        child_snow_id:  str,
        relationship:   str = "Depends on::Depended on by",
    ) -> None:
        """Create a dependency relationship between two SNOW tickets."""
        logger.info(
            "[SNOW] Linked %s → %s (%s)", parent_snow_id, child_snow_id, relationship
        )

    async def get_ticket_state(self, snow_id: str) -> dict:
        """Fetch current state of a ServiceNow ticket."""
        return {"sys_id": snow_id, "state": "In Progress", "updated_on": _now_str()}

    @staticmethod
    def _risk_to_priority(risk: str) -> str:
        return {"High": "1 - Critical", "Medium": "2 - High", "Low": "3 - Moderate"}.get(risk, "3 - Moderate")


# ─────────────────────────────────────────────────────────────────────────────
# Notification Adapters
# ─────────────────────────────────────────────────────────────────────────────

class TeamsNotifier:
    """Sends Adaptive Cards to Microsoft Teams via incoming webhook."""

    def __init__(self, default_webhook: str = "https://outlook.office.com/webhook/PLACEHOLDER") -> None:
        self._default_webhook = default_webhook

    async def send(
        self,
        recipient_webhook: str,
        subject:           str,
        body_lines:        list[str],
        ticket:            TicketRecord,
        level:             EscalationLevel,
    ) -> bool:
        card = self._build_card(subject, body_lines, ticket, level)
        # ── MOCK send ─────────────────────────────────────────────────────────
        logger.info(
            "[TEAMS] %s → '%s' | ticket=%s | level=%s",
            recipient_webhook[:40], subject, ticket.ticket_id, level.value,
        )
        return True

    def _build_card(
        self,
        subject:    str,
        body_lines: list[str],
        ticket:     TicketRecord,
        level:      EscalationLevel,
    ) -> dict:
        colour = {"WARN": "Warning", "ALERT": "Attention", "BREACH": "Attention"}.get(level.value, "Default")
        facts  = [
            {"title": "Ticket",     "value": ticket.ticket_id},
            {"title": "Task",       "value": ticket.task_id},
            {"title": "Department", "value": ticket.department},
            {"title": "Risk",       "value": ticket.risk_level},
            {"title": "Status",     "value": ticket.status.value},
            {"title": "SLA",        "value": f"{ticket.sla.elapsed_pct*100:.1f}% elapsed"},
            {"title": "Deadline",   "value": ticket.sla.deadline},
        ]
        return {
            "type": "message",
            "attachments": [{
                "contentType": "application/vnd.microsoft.card.adaptive",
                "content": {
                    "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                    "type": "AdaptiveCard",
                    "version": "1.4",
                    "body": [
                        {"type": "TextBlock", "size": "Large", "weight": "Bolder",
                         "color": colour, "text": f"⚑ {subject}"},
                        {"type": "FactSet", "facts": facts},
                        {"type": "TextBlock", "wrap": True,
                         "text": "\n".join(body_lines)},
                    ],
                    "actions": [{"type": "Action.OpenUrl", "title": "Open Ticket",
                                 "url": f"https://compliance.internal/tickets/{ticket.ticket_id}"}],
                },
            }],
        }


class EmailNotifier:
    """Sends compliance alert emails (mock — swap for SendGrid / SES / SMTP)."""

    async def send(
        self,
        to_email:   str,
        subject:    str,
        body_lines: list[str],
        ticket:     TicketRecord,
        level:      EscalationLevel,
    ) -> bool:
        body = "\n".join(body_lines)
        logger.info(
            "[EMAIL] To=%s | Subject='%s' | ticket=%s | level=%s",
            to_email, subject, ticket.ticket_id, level.value,
        )
        return True


class SMSNotifier:
    """SMS stub for High-Risk CCO breach notifications."""

    async def send(self, phone: str, message: str) -> bool:
        logger.info("[SMS] To=%s | %s", phone, message[:80])
        return True


# ─────────────────────────────────────────────────────────────────────────────
# Escalation Engine
# ─────────────────────────────────────────────────────────────────────────────

class EscalationEngine:
    """
    Evaluates SLA elapsed time, determines escalation level,
    applies noise reduction, and dispatches multi-channel notifications.
    """

    def __init__(self) -> None:
        self._teams = TeamsNotifier()
        self._email = EmailNotifier()
        self._sms   = SMSNotifier()
        self._store = AlertStore()

    async def evaluate(self, ticket: TicketRecord) -> None:
        """Called on every SLA timer tick for an active ticket."""
        sla   = ticket.sla
        now   = _now_dt()
        start = _parse_dt(sla.started_at)
        dead  = _parse_dt(sla.deadline)
        total = (dead - start).total_seconds()
        elapsed = (now - start).total_seconds()
        pct   = min(elapsed / total, 1.10) if total > 0 else 0.0

        sla.elapsed_pct = round(pct, 4)

        if pct >= ESCALATION_BREACH_PCT and not sla.breach_sent:
            await self._escalate(ticket, EscalationLevel.BREACH)
            sla.breach_sent = True
            sla.breached    = True
        elif pct >= ESCALATION_WARN_PCT and not sla.alert_sent:
            await self._escalate(ticket, EscalationLevel.ALERT)
            sla.alert_sent = True
        elif pct >= ESCALATION_WARN_PCT * 0.95 and not sla.warn_sent:
            # 76 % threshold buffer to avoid race with polling jitter
            await self._escalate(ticket, EscalationLevel.WARN)
            sla.warn_sent = True

        # Flush digest queue
        digest_batch = self._store.flush_digest()
        if digest_batch:
            await self._send_digest(digest_batch, ticket)

    async def _escalate(self, ticket: TicketRecord, level: EscalationLevel) -> None:
        recipient, subject, body_lines = self._build_message(ticket, level)

        for channel in self._channels_for(level, ticket):
            if not self._store.should_send(ticket.ticket_id, level, channel):
                self._store.record_suppressed(ticket.ticket_id, level, channel)
                logger.info(
                    "[ALERT-SUPPRESSED] ticket=%s level=%s channel=%s",
                    ticket.ticket_id, level.value, channel.value,
                )
                continue

            if channel == AlertChannel.TEAMS:
                webhook = recipient.teams
                sent = await self._teams.send(webhook, subject, body_lines, ticket, level)
            elif channel == AlertChannel.EMAIL:
                sent = await self._email.send(recipient.email, subject, body_lines, ticket, level)
            elif channel == AlertChannel.SMS:
                msg = f"BREACH ALERT: {ticket.ticket_id} | {ticket.risk_level} | {ticket.department}"
                sent = await self._sms.send("+000000000", msg)
            else:
                sent = False

            if sent:
                self._store.record_send(ticket.ticket_id, level, channel)

    def _build_message(
        self, ticket: TicketRecord, level: EscalationLevel
    ) -> tuple[Contact, str, list[str]]:
        pct  = ticket.sla.elapsed_pct * 100
        days = ticket.sla.sla_days

        if level == EscalationLevel.WARN:
            recipient = ticket.unit_head
            subject   = f"[SLA WARNING] {ticket.title} – {pct:.0f}% of {days}-day SLA elapsed"
            body      = [
                f"Dear {recipient.name},",
                f"Ticket {ticket.ticket_id} ({ticket.department}) has consumed "
                f"{pct:.0f}% of its {days}-day SLA window.",
                f"Risk Level: {ticket.risk_level}",
                f"Deadline: {ticket.sla.deadline}",
                "Please ensure the task is progressing and escalate any blockers.",
            ]
        elif level == EscalationLevel.ALERT:
            recipient = ticket.compliance_officer
            subject   = f"[SLA BREACH] {ticket.title} – SLA deadline reached"
            body      = [
                f"Dear {recipient.name},",
                f"Ticket {ticket.ticket_id} has reached 100% of its SLA.",
                f"Department: {ticket.department} | Risk: {ticket.risk_level}",
                f"Assigned to: {ticket.assigned_to.name if ticket.assigned_to else 'Unassigned'}",
                "Immediate intervention required. Ticket has been flagged for compliance review.",
            ]
        else:  # BREACH
            recipient = ticket.cco
            subject   = f"[CCO ESCALATION] HIGH-RISK SLA BREACHED – {ticket.ticket_id}"
            body      = [
                f"Dear {recipient.name} (CCO),",
                f"HIGH-RISK ticket {ticket.ticket_id} has breached its {days}-day SLA.",
                f"Elapsed: {pct:.0f}% | Department: {ticket.department}",
                f"Unit Head: {ticket.unit_head.name} | Compliance Officer: {ticket.compliance_officer.name}",
                "This ticket requires your immediate sign-off and remediation directive.",
                "All downstream dependencies have been notified.",
            ]

        return recipient, subject, body

    @staticmethod
    def _channels_for(level: EscalationLevel, ticket: TicketRecord) -> list[AlertChannel]:
        if level == EscalationLevel.WARN:
            return [AlertChannel.TEAMS, AlertChannel.EMAIL]
        if level == EscalationLevel.ALERT:
            return [AlertChannel.TEAMS, AlertChannel.EMAIL]
        # BREACH — add SMS for High Risk
        channels = [AlertChannel.TEAMS, AlertChannel.EMAIL]
        if ticket.risk_level == "High":
            channels.append(AlertChannel.SMS)
        return channels

    async def _send_digest(self, batch: list[dict], ticket: TicketRecord) -> None:
        logger.info(
            "[DIGEST] Flushing %d queued low-severity alerts for ticket %s",
            len(batch), ticket.ticket_id,
        )


# ─────────────────────────────────────────────────────────────────────────────
# State Machine
# ─────────────────────────────────────────────────────────────────────────────

class TicketStateMachine:
    """
    Enforces valid transitions, records audit trail, and persists state.
    """

    def __init__(self, ticket: TicketRecord, store: TicketStore) -> None:
        self._ticket = ticket
        self._store  = store

    @property
    def ticket(self) -> TicketRecord:
        return self._ticket

    def can_transition(self, event: TransitionEvent) -> bool:
        allowed = TRANSITIONS.get(self._ticket.status, {})
        return event in allowed

    def transition(
        self,
        event:  TransitionEvent,
        actor:  str = "system",
        note:   str = "",
    ) -> TicketStatus:
        allowed = TRANSITIONS.get(self._ticket.status, {})
        if event not in allowed:
            raise InvalidTransitionError(
                f"Event '{event.value}' is not valid from status '{self._ticket.status.value}'. "
                f"Allowed: {[e.value for e in allowed]}"
            )

        prev   = self._ticket.status
        new    = allowed[event]
        now    = _now_str()

        self._ticket.status     = new
        self._ticket.updated_at = now

        if new == TicketStatus.CLOSED:
            self._ticket.closed_at = now

        self._ticket.audit_trail.append(AuditEntry(
            timestamp=now,
            event=event.value,
            from_status=prev.value,
            to_status=new.value,
            actor=actor,
            note=note,
        ))

        self._store.save(self._ticket)

        logger.info(
            "[FSM] ticket=%s | %s ──[%s]──► %s | actor=%s",
            self._ticket.ticket_id, prev.value, event.value, new.value, actor,
        )
        return new

    def add_dependency(self, dep: Dependency, actor: str = "system") -> None:
        self._ticket.dependencies.append(dep)
        self._ticket.updated_at = _now_str()
        self._ticket.audit_trail.append(AuditEntry(
            timestamp=_now_str(),
            event="DEPENDENCY_ADDED",
            from_status=self._ticket.status.value,
            to_status=self._ticket.status.value,
            actor=actor,
            note=f"Depends on task {dep.task_id} (SNOW: {dep.snow_id})",
        ))
        self._store.save(self._ticket)

    def resolve_dependency(self, task_id: str, actor: str = "system") -> None:
        for dep in self._ticket.dependencies:
            if dep.task_id == task_id:
                dep.resolved = True
        self._ticket.updated_at = _now_str()
        self._store.save(self._ticket)


class InvalidTransitionError(Exception):
    pass


# ─────────────────────────────────────────────────────────────────────────────
# SLA Timer  (durable async loop)
# ─────────────────────────────────────────────────────────────────────────────

class SLATimer:
    """
    Long-running async loop per ticket.
    Checks elapsed time every POLL interval and delegates to EscalationEngine.
    Stops when the ticket reaches a terminal state.
    """

    TERMINAL = {TicketStatus.CLOSED, TicketStatus.REJECTED}

    def __init__(
        self,
        ticket_id:   str,
        store:       TicketStore,
        escalation:  EscalationEngine,
        poll_sec:    int = SLA_POLL_INTERVAL_SEC,
    ) -> None:
        self._ticket_id = ticket_id
        self._store     = store
        self._escalation = escalation
        self._poll_sec  = poll_sec
        self._task:     asyncio.Task | None = None

    def start(self) -> None:
        self._task = asyncio.ensure_future(self._run())
        logger.info("[SLA-TIMER] Started for ticket %s", self._ticket_id)

    def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            logger.info("[SLA-TIMER] Stopped for ticket %s", self._ticket_id)

    async def _run(self) -> None:
        while True:
            raw = self._store.load(self._ticket_id)
            if raw is None:
                logger.warning("[SLA-TIMER] Ticket %s not found; stopping.", self._ticket_id)
                return

            status = TicketStatus(raw["status"])
            if status in self.TERMINAL:
                logger.info("[SLA-TIMER] Ticket %s terminal (%s); stopping.", self._ticket_id, status.value)
                return

            # Only evaluate SLA for active (post-APPROVED) tickets
            if status not in {TicketStatus.DRAFT, TicketStatus.REJECTED}:
                ticket = _deserialise_ticket(raw)
                await self._escalation.evaluate(ticket)
                self._store.save(ticket)   # persist updated sla fields

            await asyncio.sleep(self._poll_sec)


# ─────────────────────────────────────────────────────────────────────────────
# Task Manager  (public facade)
# ─────────────────────────────────────────────────────────────────────────────

class TaskManager:
    """
    Durable compliance task manager.

    Usage::

        manager = TaskManager()

        ticket = manager.create_ticket(
            obligation_id="ob-001",
            task_id="task-abc",
            department="IT",
            title="Implement AES-256 Encryption",
            description="...",
            risk_level="High",
            unit_head=Contact("Marcus Okonkwo", "ciso@bank.org", "https://teams.webhook/ciso"),
            compliance_officer=Contact("Simone Leclerc", "cco@bank.org", "https://teams.webhook/cco"),
            cco=Contact("Board CCO", "board.cco@bank.org", "https://teams.webhook/board"),
        )
        await manager.approve(ticket.ticket_id)
        await manager.assign(ticket.ticket_id, assignee=Contact(...))
    """

    def __init__(
        self,
        state_dir:    Path = STATE_DIR,
        snow_client:  ServiceNowClient | None = None,
        poll_sec:     int = SLA_POLL_INTERVAL_SEC,
        cosmos_conn:  str | None = None,
        cosmos_db:    str = "compliancedb",
        cosmos_container: str = "task_registry",
    ) -> None:
        self._store      = TicketStore(state_dir)
        self._snow       = snow_client or ServiceNowClient()
        self._escalation = EscalationEngine()
        self._poll_sec   = poll_sec
        self._timers:    dict[str, SLATimer] = {}

        # Optional async Cosmos-backed store
        self._cosmos_store: CosmosTicketStore | None = None
        if cosmos_conn:
            try:
                self._cosmos_store = CosmosTicketStore(cosmos_conn, cosmos_db, cosmos_container)
                try:
                    loop = asyncio.get_event_loop()
                    loop.create_task(self._cosmos_store.init())
                except RuntimeError:
                    # no loop running yet; init can be awaited by caller
                    pass
            except Exception:
                logger.exception("Failed to initialise CosmosTicketStore")

    # ── Ticket creation ───────────────────────────────────────────────────────

    def create_ticket(
        self,
        obligation_id:       str,
        task_id:             str,
        department:          str,
        title:               str,
        description:         str,
        risk_level:          str,
        unit_head:           Contact,
        compliance_officer:  Contact,
        cco:                 Contact,
        metadata:            dict[str, Any] | None = None,
    ) -> TicketRecord:
        """Create a new ticket in DRAFT state and persist it."""
        if risk_level not in SLA_DAYS:
            raise ValueError(f"risk_level must be one of {list(SLA_DAYS)}; got '{risk_level}'.")

        ticket_id = _new_id()
        now       = _now_dt()
        sla_days  = SLA_DAYS[risk_level]
        deadline  = now + timedelta(days=sla_days)
        warn_at   = now + timedelta(seconds=sla_days * 86400 * ESCALATION_WARN_PCT)

        sla = SLARecord(
            risk_level=risk_level,
            sla_days=sla_days,
            started_at=now.isoformat(),
            deadline=deadline.isoformat(),
            warn_at=warn_at.isoformat(),
        )

        ticket = TicketRecord(
            ticket_id=ticket_id,
            obligation_id=obligation_id,
            task_id=task_id,
            department=department,
            title=title,
            description=description,
            risk_level=risk_level,
            status=TicketStatus.DRAFT,
            assigned_to=None,
            unit_head=unit_head,
            compliance_officer=compliance_officer,
            cco=cco,
            sla=sla,
            snow_ticket_id=None,
            dependencies=[],
            audit_trail=[AuditEntry(
                timestamp=now.isoformat(),
                event="CREATED",
                from_status="",
                to_status=TicketStatus.DRAFT.value,
                actor="system",
                note="Ticket created by TaskManager.",
            )],
            created_at=now.isoformat(),
            updated_at=now.isoformat(),
            metadata=metadata or {},
        )

        self._store.save(ticket)
        # Persist to Cosmos asynchronously if available
        if self._cosmos_store:
            try:
                loop = asyncio.get_event_loop()
                loop.create_task(self._cosmos_store.save_async(ticket))
            except Exception:
                logger.exception("Failed to schedule cosmos save for ticket %s", ticket_id)

        logger.info("[TM] Created ticket %s | risk=%s | sla=%d days", ticket_id, risk_level, sla_days)
        return ticket

    async def create_task(
        self,
        obligation_id: str,
        department: str,
        assignee: str | None = None,
        assignee_email: str | None = None,
        title: str = "",
        description: str = "",
        risk_level: str = "Medium",
        actor: str = "system",
    ) -> TicketRecord:
        """Compatibility helper: create ticket, open ServiceNow ticket, optionally assign.

        Returns the persisted TicketRecord with `snow_ticket_id` populated.
        """
        # Build placeholder contacts
        unit_head = Contact("Unit Head", f"unit.head@{department.lower().replace(' ', '')}.bank.org", "https://teams.webhook/unit")
        compliance_officer = Contact("Compliance Officer", "compliance@bank.org", "https://teams.webhook/cco")
        cco = Contact("Chief Compliance Officer", "cco@bank.org", "https://teams.webhook/cco")

        ticket = self.create_ticket(
            obligation_id=obligation_id,
            task_id=_new_id(),
            department=department,
            title=title or description or f"Compliance task — {obligation_id}",
            description=description or title or "",
            risk_level=risk_level,
            unit_head=unit_head,
            compliance_officer=compliance_officer,
            cco=cco,
        )

        # Approve -> opens ServiceNow ticket and starts SLA timer
        ticket = await self.approve(ticket.ticket_id, actor=actor)

        # Optionally assign
        if assignee or assignee_email:
            assignee_contact = Contact(assignee or assignee_email.split("@")[0], assignee_email or f"{assignee}@bank.org", "https://teams.webhook/assignee")
            ticket = await self.assign(ticket.ticket_id, assignee_contact, actor=actor)

        return ticket

    # ── Lifecycle transitions ─────────────────────────────────────────────────

    async def approve(self, ticket_id: str, actor: str = "approver") -> TicketRecord:
        """DRAFT → APPROVED. Opens a ServiceNow ticket and starts the SLA timer."""
        ticket = await self._load_async(ticket_id)
        fsm    = TicketStateMachine(ticket, self._store)
        fsm.transition(TransitionEvent.APPROVE, actor, "Ticket approved.")

        # Open ServiceNow ticket
        snow_id = await self._snow.open_ticket(ticket)
        ticket.snow_ticket_id = snow_id
        self._store.save(ticket)
        if self._cosmos_store:
            try:
                await self._cosmos_store.save_async(ticket)
            except Exception:
                logger.exception("Cosmos save failed on approve for %s", ticket_id)

        # Start SLA timer
        self._start_timer(ticket_id)
        return ticket

    async def reject(self, ticket_id: str, actor: str = "reviewer", reason: str = "") -> TicketRecord:
        """APPROVED/VALIDATING → DRAFT (or DRAFT → REJECTED)."""
        ticket = await self._load_async(ticket_id)
        fsm    = TicketStateMachine(ticket, self._store)
        fsm.transition(TransitionEvent.REJECT, actor, reason or "Rejected.")
        if ticket.snow_ticket_id:
            await self._snow.update_ticket(ticket.snow_ticket_id, "Rejected", reason)
        # Persist
        self._store.save(ticket)
        if self._cosmos_store:
            try:
                await self._cosmos_store.save_async(ticket)
            except Exception:
                logger.exception("Cosmos save failed on reject for %s", ticket_id)
        return ticket

    async def assign(
        self,
        ticket_id: str,
        assignee:  Contact,
        actor:     str = "manager",
    ) -> TicketRecord:
        """APPROVED → ASSIGNED."""
        ticket = await self._load_async(ticket_id)
        fsm    = TicketStateMachine(ticket, self._store)
        ticket.assigned_to = assignee
        fsm.transition(TransitionEvent.ASSIGN, actor, f"Assigned to {assignee.name}.")
        if ticket.snow_ticket_id:
            await self._snow.update_ticket(
                ticket.snow_ticket_id, "Assigned", f"Assigned to {assignee.email}"
            )
        # Persist
        self._store.save(ticket)
        if self._cosmos_store:
            try:
                await self._cosmos_store.save_async(ticket)
            except Exception:
                logger.exception("Cosmos save failed on assign for %s", ticket_id)
        return ticket

    async def unassign(self, ticket_id: str, actor: str = "manager", reason: str = "") -> TicketRecord:
        """ASSIGNED → DRAFT."""
        ticket = await self._load_async(ticket_id)
        fsm    = TicketStateMachine(ticket, self._store)
        ticket.assigned_to = None
        fsm.transition(TransitionEvent.UNASSIGN, actor, reason or "Unassigned.")
        # Persist
        self._store.save(ticket)
        if self._cosmos_store:
            try:
                await self._cosmos_store.save_async(ticket)
            except Exception:
                logger.exception("Cosmos save failed on unassign for %s", ticket_id)
        return ticket

    async def start(self, ticket_id: str, actor: str = "assignee") -> TicketRecord:
        """ASSIGNED → IN_PROGRESS."""
        ticket = await self._load_async(ticket_id)
        fsm    = TicketStateMachine(ticket, self._store)
        fsm.transition(TransitionEvent.START, actor, "Work started.")
        if ticket.snow_ticket_id:
            await self._snow.update_ticket(ticket.snow_ticket_id, "In Progress")
        # Persist
        self._store.save(ticket)
        if self._cosmos_store:
            try:
                await self._cosmos_store.save_async(ticket)
            except Exception:
                logger.exception("Cosmos save failed on start for %s", ticket_id)
        return ticket

    async def block(
        self,
        ticket_id:   str,
        dep_task_id: str,
        dep_snow_id: str | None = None,
        description: str = "",
        actor:       str = "system",
    ) -> TicketRecord:
        """
        Mark IN_PROGRESS ticket as blocked by a dependency.
        Creates a cross-functional dependency and links it in ServiceNow.
        """
        ticket = await self._load_async(ticket_id)
        fsm    = TicketStateMachine(ticket, self._store)
        dep    = Dependency(
            task_id=dep_task_id,
            snow_id=dep_snow_id,
            description=description or f"Blocked by {dep_task_id}",
        )
        fsm.add_dependency(dep, actor)
        fsm.transition(TransitionEvent.BLOCK, actor, f"Blocked on {dep_task_id}.")

        if ticket.snow_ticket_id and dep_snow_id:
            await self._snow.link_dependency(ticket.snow_ticket_id, dep_snow_id)
        # Persist
        self._store.save(ticket)
        if self._cosmos_store:
            try:
                await self._cosmos_store.save_async(ticket)
            except Exception:
                logger.exception("Cosmos save failed on block for %s", ticket_id)
        return ticket

    async def unblock(
        self,
        ticket_id:   str,
        dep_task_id: str,
        actor:       str = "system",
    ) -> TicketRecord:
        """Resolve a dependency; if all resolved, stays IN_PROGRESS."""
        ticket = await self._load_async(ticket_id)
        fsm    = TicketStateMachine(ticket, self._store)
        fsm.resolve_dependency(dep_task_id, actor)
        fsm.transition(TransitionEvent.UNBLOCK, actor, f"Dependency {dep_task_id} resolved.")
        # Persist
        self._store.save(ticket)
        if self._cosmos_store:
            try:
                await self._cosmos_store.save_async(ticket)
            except Exception:
                logger.exception("Cosmos save failed on unblock for %s", ticket_id)
        return ticket

    async def complete(self, ticket_id: str, actor: str = "assignee") -> TicketRecord:
        """IN_PROGRESS → COMPLETED."""
        ticket = await self._load_async(ticket_id)
        fsm    = TicketStateMachine(ticket, self._store)
        fsm.transition(TransitionEvent.COMPLETE, actor, "Work completed; awaiting validation.")
        if ticket.snow_ticket_id:
            await self._snow.update_ticket(ticket.snow_ticket_id, "Completed")
        # Persist
        self._store.save(ticket)
        if self._cosmos_store:
            try:
                await self._cosmos_store.save_async(ticket)
            except Exception:
                logger.exception("Cosmos save failed on complete for %s", ticket_id)
        return ticket

    async def validate(self, ticket_id: str, actor: str = "validator") -> TicketRecord:
        """COMPLETED → VALIDATING."""
        ticket = await self._load_async(ticket_id)
        fsm    = TicketStateMachine(ticket, self._store)
        fsm.transition(TransitionEvent.VALIDATE, actor, "Validation check initiated.")
        # Persist
        self._store.save(ticket)
        if self._cosmos_store:
            try:
                await self._cosmos_store.save_async(ticket)
            except Exception:
                logger.exception("Cosmos save failed on validate for %s", ticket_id)
        return ticket

    async def close(self, ticket_id: str, actor: str = "compliance") -> TicketRecord:
        """VALIDATING / BREACHED → CLOSED. Stops SLA timer."""
        ticket = await self._load_async(ticket_id)
        fsm    = TicketStateMachine(ticket, self._store)
        fsm.transition(TransitionEvent.CLOSE, actor, "Ticket closed.")
        self._stop_timer(ticket_id)
        if ticket.snow_ticket_id:
            await self._snow.update_ticket(ticket.snow_ticket_id, "Closed")
        # Persist
        self._store.save(ticket)
        if self._cosmos_store:
            try:
                await self._cosmos_store.save_async(ticket)
            except Exception:
                logger.exception("Cosmos save failed on close for %s", ticket_id)
        return ticket

    # ── Cross-functional dependency wiring ────────────────────────────────────

    async def link_cross_functional(
        self,
        parent_ticket_id: str,
        child_ticket_id:  str,
        actor:            str = "system",
    ) -> None:
        """
        Link two tickets as cross-functional (parent BLOCKS child).
        Both must exist in the store.
        """
        parent = await self._load_async(parent_ticket_id)
        child  = await self._load_async(child_ticket_id)

        dep = Dependency(
            task_id=child.task_id,
            snow_id=child.snow_ticket_id,
            description=f"Cross-functional dependency from {parent.department} → {child.department}",
        )
        fsm = TicketStateMachine(parent, self._store)
        fsm.add_dependency(dep, actor)

        if parent.snow_ticket_id and child.snow_ticket_id:
            await self._snow.link_dependency(parent.snow_ticket_id, child.snow_ticket_id)
        # Persist parent with new dependency
        self._store.save(parent)
        if self._cosmos_store:
            try:
                await self._cosmos_store.save_async(parent)
            except Exception:
                logger.exception("Cosmos save failed on cross-functional link for %s", parent_ticket_id)

        logger.info(
            "[TM] Cross-functional link: %s → %s", parent_ticket_id, child_ticket_id
        )

    # ── Rework (triggered by validator.py) ────────────────────────────────────

    async def trigger_rework(
        self,
        ticket_id:    str,
        failed_checks: list[str],
        actor:         str = "validator",
    ) -> TicketRecord:
        """
        Called by the validation layer on FAIL.
        Resets SLA to 2 hours and re-opens the ticket.
        """
        ticket = self._load(ticket_id)
        fsm    = TicketStateMachine(ticket, self._store)
        fsm.transition(
            TransitionEvent.REJECT, actor,
            f"Validation failed on: {', '.join(failed_checks)}. Rework triggered."
        )

        # Reset SLA to 2-hour rework window
        now      = _now_dt()
        deadline = now + timedelta(hours=2)
        ticket.sla.started_at = now.isoformat()
        ticket.sla.deadline   = deadline.isoformat()
        ticket.sla.warn_at    = (now + timedelta(minutes=90)).isoformat()
        ticket.sla.warn_sent  = False
        ticket.sla.alert_sent = False
        ticket.sla.breach_sent= False
        ticket.sla.elapsed_pct= 0.0
        self._store.save(ticket)
        if self._cosmos_store:
            try:
                await self._cosmos_store.save_async(ticket)
            except Exception:
                logger.exception("Cosmos save failed on trigger_rework for %s", ticket_id)

        if ticket.snow_ticket_id:
            await self._snow.update_ticket(
                ticket.snow_ticket_id,
                "In Progress",
                f"Rework required: {', '.join(failed_checks)}",
            )

        # Re-escalate immediately so unit head is aware
        await self._escalation._escalate(ticket, EscalationLevel.WARN)

        logger.info("[TM] Rework triggered for ticket %s | new SLA deadline=%s", ticket_id, deadline.isoformat())
        return ticket

    # ── Internal async helpers for Cosmos-backed persistence ─────────────
    async def _load_async(self, ticket_id: str) -> TicketRecord:
        """Load ticket from Cosmos if available, otherwise fall back to file store."""
        if self._cosmos_store:
            try:
                raw = await self._cosmos_store.load_async(ticket_id)
                if raw:
                    return _deserialise_ticket(raw)
            except Exception:
                logger.exception("Cosmos load failed for %s, falling back to file store", ticket_id)

        # Fallback synchronous load
        raw = self._store.load(ticket_id)
        if raw is None:
            raise KeyError(f"Ticket '{ticket_id}' not found.")
        return _deserialise_ticket(raw)

    # ── Query helpers ─────────────────────────────────────────────────────────

    def get(self, ticket_id: str) -> TicketRecord:
        return self._load(ticket_id)

    def list_tickets(self) -> list[dict[str, Any]]:
        """Return a summary list of all persisted tickets."""
        results = []
        for tid in self._store.all_ids():
            raw = self._store.load(tid)
            if raw:
                results.append({
                    "ticket_id":   raw["ticket_id"],
                    "status":      raw["status"],
                    "risk_level":  raw["risk_level"],
                    "department":  raw["department"],
                    "sla_pct":     raw["sla"].get("elapsed_pct", 0),
                    "deadline":    raw["sla"]["deadline"],
                })
        return results

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _load(self, ticket_id: str) -> TicketRecord:
        raw = self._store.load(ticket_id)
        if raw is None:
            raise KeyError(f"Ticket '{ticket_id}' not found.")
        return _deserialise_ticket(raw)

    def _start_timer(self, ticket_id: str) -> None:
        # If using Cosmos-backed store, SLA checks should be handled by distributed scanner
        if self._cosmos_store:
            logger.debug("[TM] Cosmos store present; skipping in-process SLA timer for %s", ticket_id)
            return

        if ticket_id not in self._timers:
            timer = SLATimer(ticket_id, self._store, self._escalation, self._poll_sec)
            self._timers[ticket_id] = timer
            timer.start()

    def _stop_timer(self, ticket_id: str) -> None:
        timer = self._timers.pop(ticket_id, None)
        if timer:
            timer.stop()

    async def check_sla_breaches(self) -> None:
        """Distributed SLA scanner: query Cosmos for due SLAs and evaluate them."""
        if not self._cosmos_store:
            return

        now_iso = _now_str()
        try:
            rows = await self._cosmos_store.query_due_slas(now_iso)
        except Exception:
            logger.exception("Cosmos query_due_slas failed")
            return

        for raw in rows:
            try:
                ticket = _deserialise_ticket(raw)
                await self._escalation.evaluate(ticket)
                # Persist updated SLA fields
                self._store.save(ticket)
                try:
                    await self._cosmos_store.save_async(ticket)
                except Exception:
                    logger.exception("Cosmos save failed during SLA scan for %s", ticket.ticket_id)
            except Exception:
                logger.exception("Error evaluating SLA for ticket in scan")

    async def start_sla_watcher(self) -> None:
        """Start a background watcher task that periodically checks Cosmos for SLA breaches."""
        if not self._cosmos_store:
            logger.debug("No Cosmos store configured; SLA watcher not started.")
            return

        async def _loop() -> None:
            while True:
                await self.check_sla_breaches()
                await asyncio.sleep(self._poll_sec)

        asyncio.create_task(_loop())


# ─────────────────────────────────────────────────────────────────────────────
# Deserialisation helper
# ─────────────────────────────────────────────────────────────────────────────

def _deserialise_ticket(raw: dict) -> TicketRecord:
    def _contact(d: dict | None) -> Contact | None:
        return Contact(**d) if d else None

    sla_d = raw["sla"]
    sla = SLARecord(
        risk_level=sla_d["risk_level"],
        sla_days=sla_d["sla_days"],
        started_at=sla_d["started_at"],
        deadline=sla_d["deadline"],
        warn_at=sla_d["warn_at"],
        warn_sent=sla_d.get("warn_sent", False),
        alert_sent=sla_d.get("alert_sent", False),
        breach_sent=sla_d.get("breach_sent", False),
        breached=sla_d.get("breached", False),
        elapsed_pct=sla_d.get("elapsed_pct", 0.0),
    )

    return TicketRecord(
        ticket_id=raw["ticket_id"],
        obligation_id=raw["obligation_id"],
        task_id=raw["task_id"],
        department=raw["department"],
        title=raw["title"],
        description=raw["description"],
        risk_level=raw["risk_level"],
        status=TicketStatus(raw["status"]),
        assigned_to=_contact(raw.get("assigned_to")),
        unit_head=Contact(**raw["unit_head"]),
        compliance_officer=Contact(**raw["compliance_officer"]),
        cco=Contact(**raw["cco"]),
        sla=sla,
        snow_ticket_id=raw.get("snow_ticket_id"),
        dependencies=[Dependency(**d) for d in raw.get("dependencies", [])],
        audit_trail=[AuditEntry(**a) for a in raw.get("audit_trail", [])],
        created_at=raw["created_at"],
        updated_at=raw["updated_at"],
        closed_at=raw.get("closed_at"),
        metadata=raw.get("metadata", {}),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Utility helpers
# ─────────────────────────────────────────────────────────────────────────────

def _new_id()  -> str:             return str(uuid.uuid4())
def _now_dt()  -> datetime:        return datetime.now(tz=timezone.utc)
def _now_str() -> str:             return _now_dt().isoformat()
def _parse_dt(s: str) -> datetime: return datetime.fromisoformat(s)


# ─────────────────────────────────────────────────────────────────────────────
# Module-level singleton
# ─────────────────────────────────────────────────────────────────────────────

_DEFAULT_MANAGER: TaskManager | None = None


def get_manager() -> TaskManager:
    global _DEFAULT_MANAGER
    if _DEFAULT_MANAGER is None:
        _DEFAULT_MANAGER = TaskManager()
    return _DEFAULT_MANAGER


# ─────────────────────────────────────────────────────────────────────────────
# CLI demo
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import json as _json

    async def _demo() -> None:
        manager = TaskManager(poll_sec=2)   # fast poll for demo

        unit_head = Contact("Marcus Okonkwo", "ciso@bank.org",    "https://teams.webhook/ciso")
        cco_co    = Contact("Simone Leclerc", "cco@bank.org",     "https://teams.webhook/cco")
        cco       = Contact("Board CCO",      "board.cco@bank.org","https://teams.webhook/board")
        assignee  = Contact("Alice Dev",      "alice@bank.org",   "https://teams.webhook/alice")

        # Create High-Risk ticket
        t = manager.create_ticket(
            obligation_id="ob-001",
            task_id="task-xyz",
            department="IT",
            title="Implement AES-256 Encryption",
            description="All systems must adopt AES-256 within 7 days.",
            risk_level="High",
            unit_head=unit_head,
            compliance_officer=cco_co,
            cco=cco,
        )
        print(f"\n✔ Created ticket: {t.ticket_id}\n")

        # Full lifecycle
        await manager.approve(t.ticket_id)
        await manager.assign(t.ticket_id, assignee)
        await manager.start(t.ticket_id, actor="alice")

        # Simulate cross-functional dependency
        t2 = manager.create_ticket(
            obligation_id="ob-001",
            task_id="task-kyc",
            department="Retail Banking",
            title="KYC Data Encryption Alignment",
            description="KYC data stores must align with the new encryption standard.",
            risk_level="Medium",
            unit_head=Contact("Alexandra Patel", "chief.retail@bank.org", "https://teams.webhook/retail"),
            compliance_officer=cco_co,
            cco=cco,
        )
        await manager.approve(t2.ticket_id)
        await manager.link_cross_functional(t.ticket_id, t2.ticket_id)

        await asyncio.sleep(3)   # let timer tick once

        await manager.complete(t.ticket_id, actor="alice")
        await manager.validate(t.ticket_id, actor="validator")
        await manager.close(t.ticket_id,    actor="compliance")

        print("\n── Final ticket state ──────────────────────────────────")
        print(_json.dumps(manager.get(t.ticket_id).to_dict(), indent=2, default=str))

        print("\n── All tickets ─────────────────────────────────────────")
        print(_json.dumps(manager.list_tickets(), indent=2, default=str))

    asyncio.run(_demo())