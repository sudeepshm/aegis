"""
workflow/task_manager.py
========================
Lifecycle management, SLA timers, dependency tracking, and escalation engine.

Architecture
────────────
  ┌──────────────────────────────────────────┐
  │           TaskManager                    │
  │  ┌────────────────────────────────────┐  │
  │  │  ServiceNow Integration           │  │
  │  │  OAuth2 + Table API               │  │
  │  └────────────────────────────────────┘  │
  │  ┌────────────────────────────────────┐  │
  │  │  SLA Timer Engine                 │  │
  │  │  Deadline tracking + auto-escalate│  │
  │  └────────────────────────────────────┘  │
  │  ┌────────────────────────────────────┐  │
  │  │  Dependency Resolver              │  │
  │  │  DAG-based task ordering          │  │
  │  └────────────────────────────────────┘  │
  │  ┌────────────────────────────────────┐  │
  │  │  Escalation Engine                │  │
  │  │  Hierarchy-based auto-escalation  │  │
  │  └────────────────────────────────────┘  │
  └──────────────────────────────────────────┘

Dependencies
────────────
  pip install httpx
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────────
# Enumerations
# ──────────────────────────────────────────────────────────────────────────────

class TaskState(str, Enum):
    NEW         = "New"
    IN_PROGRESS = "In Progress"
    PENDING     = "Pending"
    ON_HOLD     = "On Hold"
    RESOLVED    = "Resolved"
    CLOSED      = "Closed"
    ESCALATED   = "Escalated"
    BREACHED    = "SLA Breached"


class EscalationLevel(str, Enum):
    L1 = "L1_Department_Head"
    L2 = "L2_Compliance_Officer"
    L3 = "L3_Board_Level"


# ──────────────────────────────────────────────────────────────────────────────
# Data Structures
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class SLARecord:
    """Tracks SLA state for a task."""
    sla_hours:     int
    started_at:    datetime
    deadline:      datetime
    breached:      bool = False
    paused:        bool = False
    pause_start:   Optional[datetime] = None
    total_paused:  timedelta = field(default_factory=timedelta)

    @property
    def remaining_seconds(self) -> float:
        if self.breached:
            return 0.0
        now = datetime.now(tz=timezone.utc)
        effective_deadline = self.deadline + self.total_paused
        remaining = (effective_deadline - now).total_seconds()
        return max(remaining, 0.0)

    @property
    def is_breached(self) -> bool:
        return self.remaining_seconds <= 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "sla_hours":     self.sla_hours,
            "started_at":    self.started_at.isoformat(),
            "deadline":      self.deadline.isoformat(),
            "breached":      self.breached or self.is_breached,
            "remaining_sec": round(self.remaining_seconds, 0),
        }


@dataclass
class ComplianceTask:
    """A compliance task with full lifecycle tracking."""
    task_id:         str
    obligation_id:   str
    department:      str
    assignee:        str
    assignee_email:  str
    description:     str
    priority:        str
    state:           TaskState = TaskState.NEW
    sla:             Optional[SLARecord] = None
    dependencies:    list[str] = field(default_factory=list)
    escalation_level: EscalationLevel = EscalationLevel.L1
    snow_sys_id:     str = ""
    snow_number:     str = ""
    history:         list[dict[str, Any]] = field(default_factory=list)
    created_at:      str = field(
        default_factory=lambda: datetime.now(tz=timezone.utc).isoformat()
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id":          self.task_id,
            "obligation_id":   self.obligation_id,
            "department":      self.department,
            "assignee":        self.assignee,
            "state":           self.state.value,
            "priority":        self.priority,
            "sla":             self.sla.to_dict() if self.sla else None,
            "dependencies":    self.dependencies,
            "escalation_level": self.escalation_level.value,
            "snow_number":     self.snow_number,
            "created_at":      self.created_at,
        }


# ──────────────────────────────────────────────────────────────────────────────
# ServiceNow Client
# ──────────────────────────────────────────────────────────────────────────────

class ServiceNowClient:
    """
    ServiceNow Table API client with OAuth2 authentication.
    Creates, updates, and queries incidents/tasks.
    """

    def __init__(self, instance_url: str = "", client_id: str = "",
                 client_secret: str = "", username: str = "",
                 password: str = ""):
        self._instance_url = instance_url.rstrip("/")
        self._client_id = client_id
        self._client_secret = client_secret
        self._username = username
        self._password = password
        self._token: Optional[str] = None
        self._http = httpx.Client(timeout=30) if instance_url else None

    def _authenticate(self) -> str:
        """Obtain OAuth2 access token from ServiceNow."""
        if self._token:
            return self._token
        if not self._http:
            raise ConnectionError("ServiceNow not configured")

        resp = self._http.post(
            f"{self._instance_url}/oauth_token.do",
            data={
                "grant_type": "password",
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "username": self._username,
                "password": self._password,
            },
        )
        resp.raise_for_status()
        self._token = resp.json()["access_token"]
        return self._token

    def create_incident(self, task: ComplianceTask) -> tuple[str, str]:
        """Create a ServiceNow incident. Returns (sys_id, number)."""
        if not self._http:
            logger.warning("ServiceNow not configured. Task not created.")
            return "", ""

        token = self._authenticate()
        payload = {
            "short_description": f"[Compliance] {task.description[:200]}",
            "description": task.description,
            "urgency": "1" if "Critical" in task.priority else "2",
            "impact": "1" if "Critical" in task.priority else "2",
            "assignment_group": task.department,
            "assigned_to": task.assignee_email,
            "category": "Compliance",
            "subcategory": "Regulatory",
            "correlation_id": task.task_id,
        }

        resp = self._http.post(
            f"{self._instance_url}/api/now/table/incident",
            headers={"Authorization": f"Bearer {token}",
                     "Content-Type": "application/json"},
            json=payload,
        )
        resp.raise_for_status()
        result = resp.json()["result"]
        sys_id = result["sys_id"]
        number = result["number"]
        logger.info("ServiceNow incident created: %s (%s)", number, sys_id)
        return sys_id, number

    def update_incident(self, sys_id: str, updates: dict[str, Any]) -> None:
        """Update an existing ServiceNow incident."""
        if not self._http or not sys_id:
            return

        token = self._authenticate()
        self._http.patch(
            f"{self._instance_url}/api/now/table/incident/{sys_id}",
            headers={"Authorization": f"Bearer {token}",
                     "Content-Type": "application/json"},
            json=updates,
        )
        logger.info("ServiceNow incident updated: %s", sys_id)

    def close(self) -> None:
        if self._http:
            self._http.close()


# ──────────────────────────────────────────────────────────────────────────────
# SLA Engine
# ──────────────────────────────────────────────────────────────────────────────

class SLAEngine:
    """Manages SLA timer lifecycle: start, pause, resume, check breach."""

    def start_sla(self, sla_hours: int) -> SLARecord:
        now = datetime.now(tz=timezone.utc)
        return SLARecord(
            sla_hours=sla_hours,
            started_at=now,
            deadline=now + timedelta(hours=sla_hours),
        )

    def check_breach(self, sla: SLARecord) -> bool:
        if sla.breached:
            return True
        if sla.is_breached:
            sla.breached = True
            return True
        return False

    def pause_sla(self, sla: SLARecord) -> None:
        if not sla.paused:
            sla.paused = True
            sla.pause_start = datetime.now(tz=timezone.utc)

    def resume_sla(self, sla: SLARecord) -> None:
        if sla.paused and sla.pause_start:
            pause_duration = datetime.now(tz=timezone.utc) - sla.pause_start
            sla.total_paused += pause_duration
            sla.paused = False
            sla.pause_start = None

    def reset_sla(self, sla_hours: int) -> SLARecord:
        """Reset SLA (e.g., after rework trigger)."""
        return self.start_sla(sla_hours)


# ──────────────────────────────────────────────────────────────────────────────
# Dependency Resolver
# ──────────────────────────────────────────────────────────────────────────────

class DependencyResolver:
    """DAG-based task dependency resolution."""

    def __init__(self) -> None:
        self._graph: dict[str, list[str]] = {}  # task_id → [dependency_ids]

    def add_task(self, task_id: str, dependencies: list[str]) -> None:
        self._graph[task_id] = dependencies

    def get_ready_tasks(self, completed: set[str]) -> list[str]:
        """Return task IDs whose dependencies are all completed."""
        ready = []
        for task_id, deps in self._graph.items():
            if task_id not in completed and all(d in completed for d in deps):
                ready.append(task_id)
        return ready

    def has_circular(self) -> bool:
        """Detect circular dependencies via DFS."""
        visited: set[str] = set()
        in_stack: set[str] = set()

        def dfs(node: str) -> bool:
            visited.add(node)
            in_stack.add(node)
            for dep in self._graph.get(node, []):
                if dep in in_stack:
                    return True
                if dep not in visited and dfs(dep):
                    return True
            in_stack.discard(node)
            return False

        for task_id in self._graph:
            if task_id not in visited:
                if dfs(task_id):
                    return True
        return False


# ──────────────────────────────────────────────────────────────────────────────
# Escalation Engine
# ──────────────────────────────────────────────────────────────────────────────

class EscalationEngine:
    """Manages automatic escalation based on SLA breach and hierarchy."""

    ESCALATION_THRESHOLDS = {
        EscalationLevel.L1: 0.75,   # escalate at 75% SLA consumed
        EscalationLevel.L2: 1.00,   # escalate at 100% (breach)
        EscalationLevel.L3: 1.50,   # escalate at 150% (critical overdue)
    }

    def check_escalation(self, task: ComplianceTask) -> Optional[EscalationLevel]:
        """Check if a task needs escalation based on SLA consumption."""
        if not task.sla:
            return None

        sla = task.sla
        elapsed = (datetime.now(tz=timezone.utc) - sla.started_at).total_seconds()
        total_sla = sla.sla_hours * 3600
        consumption = elapsed / total_sla if total_sla > 0 else 1.0

        current = task.escalation_level
        for level in [EscalationLevel.L3, EscalationLevel.L2, EscalationLevel.L1]:
            threshold = self.ESCALATION_THRESHOLDS[level]
            if consumption >= threshold and self._level_order(level) > self._level_order(current):
                return level

        return None

    @staticmethod
    def _level_order(level: EscalationLevel) -> int:
        return {"L1_Department_Head": 1, "L2_Compliance_Officer": 2,
                "L3_Board_Level": 3}.get(level.value, 0)


# ──────────────────────────────────────────────────────────────────────────────
# Task Manager — Main Facade
# ──────────────────────────────────────────────────────────────────────────────

class TaskManager:
    """
    Central task lifecycle manager.

    Usage::

        manager = TaskManager(snow_instance_url="...", ...)
        task = manager.create_task(obligation_id="ob-001", ...)
        manager.check_sla_breaches()
    """

    def __init__(self, snow_instance_url: str = "",
                 snow_client_id: str = "", snow_client_secret: str = "",
                 snow_username: str = "", snow_password: str = ""):
        self._snow = ServiceNowClient(
            instance_url=snow_instance_url,
            client_id=snow_client_id,
            client_secret=snow_client_secret,
            username=snow_username,
            password=snow_password,
        )
        self._sla_engine = SLAEngine()
        self._dep_resolver = DependencyResolver()
        self._escalation = EscalationEngine()
        self._tasks: dict[str, ComplianceTask] = {}

    def create_task(self, obligation_id: str, department: str,
                    assignee: str, assignee_email: str,
                    description: str, priority: str,
                    sla_hours: int = 24,
                    dependencies: Optional[list[str]] = None) -> ComplianceTask:
        """Create a new compliance task with SLA and optional ServiceNow ticket."""
        task = ComplianceTask(
            task_id=f"task-{uuid.uuid4().hex[:12]}",
            obligation_id=obligation_id,
            department=department,
            assignee=assignee,
            assignee_email=assignee_email,
            description=description,
            priority=priority,
            sla=self._sla_engine.start_sla(sla_hours),
            dependencies=dependencies or [],
        )

        # Register dependencies
        self._dep_resolver.add_task(task.task_id, task.dependencies)

        # Create ServiceNow incident
        try:
            sys_id, number = self._snow.create_incident(task)
            task.snow_sys_id = sys_id
            task.snow_number = number
        except Exception as exc:
            logger.error("ServiceNow creation failed: %s", exc)
            task.history.append({
                "event": "snow_creation_failed",
                "error": str(exc),
                "timestamp": datetime.now(tz=timezone.utc).isoformat(),
            })

        self._tasks[task.task_id] = task
        task.history.append({
            "event": "created",
            "state": task.state.value,
            "timestamp": task.created_at,
        })

        logger.info("Task created: %s (dept=%s, SLA=%dh)",
                     task.task_id, department, sla_hours)
        return task

    def transition(self, task_id: str, new_state: TaskState,
                   actor: str = "system") -> ComplianceTask:
        """Transition a task to a new state."""
        task = self._tasks.get(task_id)
        if not task:
            raise ValueError(f"Task '{task_id}' not found")

        old_state = task.state
        task.state = new_state
        task.history.append({
            "event": "state_change",
            "from": old_state.value,
            "to": new_state.value,
            "actor": actor,
            "timestamp": datetime.now(tz=timezone.utc).isoformat(),
        })

        # Sync to ServiceNow
        if task.snow_sys_id:
            try:
                self._snow.update_incident(task.snow_sys_id, {
                    "state": self._map_state_to_snow(new_state),
                })
            except Exception as exc:
                logger.error("ServiceNow update failed: %s", exc)

        # Handle SLA pausing/resuming
        if task.sla:
            if new_state == TaskState.ON_HOLD:
                self._sla_engine.pause_sla(task.sla)
            elif old_state == TaskState.ON_HOLD:
                self._sla_engine.resume_sla(task.sla)

        logger.info("Task %s: %s → %s (by %s)",
                     task_id, old_state.value, new_state.value, actor)
        return task

    def check_sla_breaches(self) -> list[ComplianceTask]:
        """Check all active tasks for SLA breaches and trigger escalation."""
        breached: list[ComplianceTask] = []

        for task in self._tasks.values():
            if task.state in (TaskState.CLOSED, TaskState.RESOLVED):
                continue

            if task.sla and self._sla_engine.check_breach(task.sla):
                task.state = TaskState.BREACHED
                breached.append(task)

                new_level = self._escalation.check_escalation(task)
                if new_level:
                    task.escalation_level = new_level
                    task.state = TaskState.ESCALATED
                    task.history.append({
                        "event": "escalated",
                        "level": new_level.value,
                        "timestamp": datetime.now(tz=timezone.utc).isoformat(),
                    })
                    logger.warning("Task %s ESCALATED to %s (SLA breached)",
                                   task.task_id, new_level.value)

        return breached

    def get_ready_tasks(self) -> list[ComplianceTask]:
        """Get tasks whose dependencies are all completed."""
        completed = {
            tid for tid, t in self._tasks.items()
            if t.state in (TaskState.RESOLVED, TaskState.CLOSED)
        }
        ready_ids = self._dep_resolver.get_ready_tasks(completed)
        return [self._tasks[tid] for tid in ready_ids if tid in self._tasks]

    def get_task(self, task_id: str) -> Optional[ComplianceTask]:
        return self._tasks.get(task_id)

    @staticmethod
    def _map_state_to_snow(state: TaskState) -> str:
        mapping = {
            TaskState.NEW: "1", TaskState.IN_PROGRESS: "2",
            TaskState.ON_HOLD: "3", TaskState.RESOLVED: "6",
            TaskState.CLOSED: "7", TaskState.ESCALATED: "2",
            TaskState.BREACHED: "2",
        }
        return mapping.get(state, "1")

    def close(self) -> None:
        self._snow.close()
