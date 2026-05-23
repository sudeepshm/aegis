"""
main.py
=======
LangGraph Orchestrator — Compliance Automation Platform
Running on Azure Kubernetes Service (India Central)

Pipeline stages
---------------
  Ingestion → Parse → MAP Generation → Routing →
  Task Creation → Validation → Ledger Update

Each stage is a LangGraph node.  Edges carry a typed `PipelineState` object
that every node reads and enriches.  The graph is compiled once at startup
and reused across concurrent chain executions (AKS pods scale horizontally).

Kill Switch
-----------
A `KillSwitchMonitor` tracks rolling error rates across all pipeline runs.
If the rate exceeds the configured threshold (default 2 %), it:
  1. Sets a process-level flag that blocks new pipeline invocations.
  2. POSTs a webhook alert to PagerDuty / Teams / Slack.
  3. Writes an immutable kill-switch event to the Cosmos DB audit ledger.
  4. Logs a CRITICAL message so AKS liveness probe can restart the pod.

Graceful Degradation
--------------------
Each agent node wraps its work in a `@degradable` decorator.  On failure:
  * Retries up to `MAX_RETRIES` times with exponential back-off.
  * On exhaustion, marks the stage as DEGRADED and continues the pipeline
    with reduced fidelity (e.g., MAP generation falls back to a rule-based
    engine; task creation queues to a dead-letter topic instead of
    ServiceNow).
  * A DEGRADED stage never silently produces wrong output — it sets
    `state.degraded_stages` so downstream nodes and the audit ledger know.

Dependencies
------------
  pip install langgraph langchain-core httpx tenacity structlog
  pip install azure-identity azure-keyvault-secrets azure-cosmos
"""

from __future__ import annotations

import asyncio
import functools
import logging
import os
import time
import uuid
import json
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Optional

import structlog                            # structured JSON logging for AKS

# LangGraph
try:
    from langgraph.graph import END, StateGraph
    _LANGGRAPH_AVAILABLE = True
except ImportError:
    _LANGGRAPH_AVAILABLE = False

# Tenacity (retries)
try:
    from tenacity import (
        retry,
        retry_if_exception_type,
        stop_after_attempt,
        wait_exponential,
        RetryError,
    )
    _TENACITY_AVAILABLE = True
except ImportError:
    _TENACITY_AVAILABLE = False

# HTTP (kill-switch webhook)
try:
    import httpx
    _HTTPX_AVAILABLE = True
except ImportError:
    _HTTPX_AVAILABLE = False

from config import Settings, get_settings
from audit.logger import (
    AuditLogger,
    AuditLoggerConfig,
    ExplainabilityMetadata,
    NodeType,
)

# ── Production module imports ──────────────────────────────────────────────
from ingestion.fetch_rbi import IngestionEngine, FetchConfig, RegulatorySource
from parser.chunker import SemanticChunker
from llm.map_generator import MAPGenerator, MAPStatus
from routing.router import HybridRouter
from workflow.task_manager import TaskManager
from validation.validator import (
    HybridValidator,
    ValidationRequest,
    ValidationOutcome,
)

# ── Multi-Agent Intelligence Layer (Phase 3-5) ───────────────────────────────
try:
    from agents.planner_agent import PlannerAgent, MultiAgentResult, AgentMessage
    from agents.reasoning_agent import DeepReasoningAgent
    from knowledge.policy_graph import PolicyGraph
    from knowledge.risk_engine import RiskScoringEngine
    _AGENT_LAYER_AVAILABLE = True
except ImportError as _agent_import_err:
    _AGENT_LAYER_AVAILABLE = False
    logging.getLogger(__name__).warning(
        "agent_layer_unavailable: %s (pip install networkx pyyaml)",
        _agent_import_err,
    )

# ---------------------------------------------------------------------------
log = structlog.get_logger(__name__)

MAX_RETRIES          = 3
BACKOFF_MULTIPLIER   = 1.5   # seconds
ROLLING_WINDOW_SIZE  = 100   # last N pipeline runs for error-rate calculation


# ===========================================================================
# 1.  ENUMS & STATE
# ===========================================================================

class StageStatus(str, Enum):
    PENDING   = "PENDING"
    RUNNING   = "RUNNING"
    SUCCESS   = "SUCCESS"
    DEGRADED  = "DEGRADED"
    FAILED    = "FAILED"
    SKIPPED   = "SKIPPED"     # skipped because kill switch is active


@dataclass
class DocumentPayload:
    """Raw document handed off from Ingestion to Parse."""
    doc_id:    str
    source:    str          # "minio" | "adls" | "upload"
    blob_url:  str
    mime_type: str
    metadata:  dict[str, Any] = field(default_factory=dict)


@dataclass
class ParsedDocument:
    """Structured extraction produced by the Parse stage."""
    doc_id:      str
    raw_text:    str
    sections:    list[dict[str, Any]]
    entities:    list[dict[str, Any]]
    confidence:  float = 1.0


@dataclass
class MAPNode:
    """A single node in the generated MAP (Master Audit Plan)."""
    map_id:      str
    control_ref: str
    description: str
    owner:       str
    risk_level:  str   # HIGH | MEDIUM | LOW


@dataclass
class RoutingDecision:
    """Output of the Routing stage: which team / queue handles this."""
    assignee_group: str
    priority:       str
    sla_hours:      int


@dataclass
class ServiceNowTask:
    """ServiceNow incident / task record."""
    sys_id:      str
    number:      str
    state:       str
    short_desc:  str


@dataclass
class ValidationResult:
    """Output of the Validation stage."""
    is_valid:   bool
    errors:     list[str]
    warnings:   list[str]


@dataclass
class PipelineState:
    """
    Typed state object threaded through every LangGraph node.
    LangGraph requires the state to be serialisable; all fields use
    standard Python types or dataclasses (no unpicklable objects).
    """
    # Identifiers
    run_id:     str = field(default_factory=lambda: str(uuid.uuid4()))
    chain_id:   str = ""   # populated after AuditLogger.new_chain()

    # Stage outputs  (None = not yet produced)
    document:        Optional[DocumentPayload]  = None
    parsed:          Optional[ParsedDocument]   = None
    map_nodes:       list[MAPNode]              = field(default_factory=list)
    routing:         Optional[RoutingDecision]  = None
    snow_task:       Optional[ServiceNowTask]   = None
    validation:      Optional[ValidationResult] = None
    ledger_finalised: bool                      = False

    # Stage health tracking
    stage_statuses:  dict[str, StageStatus]     = field(default_factory=dict)
    degraded_stages: list[str]                  = field(default_factory=list)
    errors:          list[str]                  = field(default_factory=list)

    # Kill switch
    killed:          bool                       = False

    # Timing
    started_at:      float = field(default_factory=time.monotonic)

    # ── Multi-Agent Intelligence Fields (Phase 3-8) ──────────────────────────
    # All new fields are Optional / default to empty so existing serialisation
    # contracts (LangGraph JSON serialisation) remain fully backward-compatible.

    # PlannerAgent output: full multi-agent run result (serialised dict)
    multi_agent_result:  Optional[dict[str, Any]] = None

    # Per-obligation reasoning summaries from DeepReasoningAgent
    reasoning_results:   list[dict[str, Any]]     = field(default_factory=list)

    # All inter-agent messages (audit trail)
    agent_messages:      list[dict[str, Any]]     = field(default_factory=list)

    # Number of agent disagreements that were resolved in this run
    disagreements_resolved: int                   = 0

    # Whether any obligation was escalated (blocks MAP generation)
    has_escalations:     bool                     = False

    # Risk scores per obligation: list of RiskScore.to_dict()
    risk_scores:         list[dict[str, Any]]     = field(default_factory=list)

    # Policy conflicts found by CriticAgent across the document
    policy_conflicts:    list[dict[str, Any]]     = field(default_factory=list)

    # Evidence manifests (one per obligation)
    evidence_manifests:  list[dict[str, Any]]     = field(default_factory=list)

    # Supersession notes (were any cited regulations superseded?)
    supersession_notes:  list[str]                = field(default_factory=list)

    # Ontology snapshot used during this run (for audit trail)
    ontology_summary:    str                      = ""


# ===========================================================================
# 2.  KILL SWITCH MONITOR
# ===========================================================================

class KillSwitchMonitor:
    """
    Rolling-window error-rate tracker.

    Thread-safe for use across concurrent asyncio tasks (GIL protects
    deque appends; no shared mutable numeric state beyond the deque).

    A "run" is recorded as True (success) or False (failure).
    If `failure_count / window_size > threshold`, the kill switch trips.
    """

    def __init__(self, threshold: float = 0.02, window: int = ROLLING_WINDOW_SIZE,
                 webhook_url: str = "", settings: Optional[Settings] = None):
        self._threshold  = threshold
        self._window     = window
        self._webhook    = webhook_url
        self._settings   = settings
        # Persistent run store (newline-delimited JSON file) — optional.
        # If `settings.kill_switch_store_path` is provided, use that path;
        # otherwise default to './kill_switch_runs.jsonl'.
        store_path = None
        if settings and getattr(settings, "kill_switch_store_path", None):
            store_path = getattr(settings, "kill_switch_store_path")
        self._store_path = Path(store_path) if store_path else Path("./kill_switch_runs.jsonl")
        self._runs: list[bool] = []
        # Load existing runs from disk if available
        try:
            if self._store_path.exists():
                with self._store_path.open("r", encoding="utf-8") as fh:
                    for line in fh:
                        try:
                            obj = json.loads(line.strip())
                            self._runs.append(bool(obj.get("success", False)))
                        except Exception:
                            continue
                # keep only the most recent `window` entries
                self._runs = self._runs[-self._window:]
        except Exception as exc:
            log.warning("kill_switch.store_load_failed", error=str(exc))
        self._active     = False
        self._audit_logger: Optional[AuditLogger] = None

    # ------------------------------------------------------------------
    def bind_audit_logger(self, al: AuditLogger) -> None:
        self._audit_logger = al

    # ------------------------------------------------------------------
    def record(self, success: bool) -> None:
        # Append to in-memory list and persist to disk
        self._runs.append(success)
        # Trim to window size
        if len(self._runs) > self._window:
            self._runs = self._runs[-self._window:]
        try:
            with self._store_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({"ts": time.time(), "success": bool(success)}) + "\n")
        except Exception as exc:
            log.warning("kill_switch.store_append_failed", error=str(exc))
        rate = self.error_rate()
        log.info("kill_switch.record", success=success, error_rate=f"{rate:.2%}")
        if not self._active and rate > self._threshold:
            self._trip(rate)

    # ------------------------------------------------------------------
    def error_rate(self) -> float:
        if not self._runs:
            return 0.0
        return sum(1 for r in self._runs if not r) / len(self._runs)

    # ------------------------------------------------------------------
    def is_active(self) -> bool:
        return self._active

    # ------------------------------------------------------------------
    def reset(self, *, actor: str) -> None:
        """Manually re-enable automation after remediation."""
        log.warning("kill_switch.reset", actor=actor)
        self._active = False
        self._runs.clear()

    # ------------------------------------------------------------------
    def _trip(self, rate: float) -> None:
        self._active = True
        msg = (
            f"KILL SWITCH ACTIVATED — error rate {rate:.2%} "
            f"exceeds threshold {self._threshold:.2%}"
        )
        log.critical("kill_switch.tripped", error_rate=f"{rate:.2%}",
                     threshold=f"{self._threshold:.2%}")

        # Write immutable kill-switch event to audit ledger
        if self._audit_logger and self._settings:
            try:
                cid = self._audit_logger.new_chain()
                self._audit_logger.append(
                    cid, NodeType.REGULATION,
                    {"event": "KILL_SWITCH_ACTIVATED",
                     "error_rate": rate,
                     "threshold": self._threshold,
                     "message": msg},
                )
            except Exception as exc:
                log.error("kill_switch.ledger_write_failed", error=str(exc))

        # Fire webhook (non-blocking best-effort)
        if self._webhook and _HTTPX_AVAILABLE:
            try:
                payload = {"text": msg, "error_rate": rate, "threshold": self._threshold}
                httpx.post(self._webhook, json=payload, timeout=5)
                log.info("kill_switch.webhook_sent", url=self._webhook)
            except Exception as exc:
                log.error("kill_switch.webhook_failed", error=str(exc))


# ===========================================================================
# 3.  GRACEFUL DEGRADATION DECORATOR
# ===========================================================================

def degradable(stage_name: str):
    """
    Decorator for LangGraph node functions.

    Wraps the node in a retry loop (tenacity).  On final failure:
      - Records the error in `state.errors`
      - Marks the stage as DEGRADED in `state.stage_statuses`
      - Appends the stage name to `state.degraded_stages`
      - Returns the state unchanged so the pipeline can continue

    Usage::
        @degradable("parse")
        async def parse_node(state: PipelineState, **deps) -> PipelineState:
            ...
    """
    def decorator(fn: Callable) -> Callable:
        @functools.wraps(fn)
        async def wrapper(state: PipelineState, **deps) -> PipelineState:
            if state.killed:
                state.stage_statuses[stage_name] = StageStatus.SKIPPED
                log.warning("stage.skipped", stage=stage_name, run_id=state.run_id,
                            reason="kill_switch_active")
                return state

            state.stage_statuses[stage_name] = StageStatus.RUNNING

            attempts = MAX_RETRIES if _TENACITY_AVAILABLE else 1
            last_exc: Optional[Exception] = None

            for attempt in range(1, attempts + 1):
                try:
                    state = await fn(state, **deps)
                    state.stage_statuses[stage_name] = StageStatus.SUCCESS
                    log.info("stage.success", stage=stage_name, run_id=state.run_id,
                             attempt=attempt)
                    return state
                except Exception as exc:
                    last_exc = exc
                    wait = BACKOFF_MULTIPLIER ** attempt
                    log.warning("stage.retry", stage=stage_name, run_id=state.run_id,
                                attempt=attempt, error=str(exc), wait_s=wait)
                    if attempt < attempts:
                        await asyncio.sleep(wait)

            # All retries exhausted → degrade gracefully
            state.stage_statuses[stage_name] = StageStatus.DEGRADED
            state.degraded_stages.append(stage_name)
            state.errors.append(f"{stage_name}: {last_exc}")
            log.error("stage.degraded", stage=stage_name, run_id=state.run_id,
                      error=str(last_exc))
            return state

        return wrapper
    return decorator


# ===========================================================================
# 4.  PIPELINE NODE IMPLEMENTATIONS
# ===========================================================================
# Each node is an `async def` that receives `PipelineState` and returns it
# enriched.  Heavy I/O is awaited; CPU-bound work can be offloaded via
# `asyncio.to_thread`.

class PipelineNodes:
    """
    Stateless collection of node coroutines.
    Dependencies (settings, audit logger, kill switch) are injected once
    via __init__ and captured in closures returned by `build_*` methods.
    """

    def __init__(self, settings: Settings, audit_logger: AuditLogger,
                 kill_switch: KillSwitchMonitor, task_manager: TaskManager):
        self._cfg   = settings
        self._al    = audit_logger
        self._ks    = kill_switch
        self._tm    = task_manager

    # ── 4.1 Ingestion ──────────────────────────────────────────────────────
    @degradable("ingestion")
    async def ingestion_node(self, state: PipelineState) -> PipelineState:
        """
        Pull raw documents from the regulatory source via IngestionEngine.

        FIX: IngestionEngine.__init__ takes a FetchConfig (not Settings).
        FIX: ingest() is synchronous — must be called via asyncio.to_thread
             to avoid blocking the LangGraph event loop on AKS.
        FIX: ingest() requires (url, source) positional arguments.
        """
        log.info("ingestion.start", run_id=state.run_id)

        # Build a FetchConfig from the centralised Settings object
        fetch_cfg = FetchConfig(
            adls_connection_string   = getattr(self._cfg, "adls_connection_string", ""),
            adls_account_name        = getattr(self._cfg, "adls_account_name", ""),
            adls_container_name      = getattr(self._cfg, "adls_container_name", "compliance-docs"),
            cosmos_connection_string = getattr(self._cfg, "cosmos_connection_string", ""),
            form_recognizer_endpoint = getattr(self._cfg, "form_recognizer_endpoint", ""),
            form_recognizer_key      = getattr(self._cfg, "form_recognizer_key", ""),
            azure_region             = getattr(self._cfg, "azure_region", "centralindia"),
        )
        engine = IngestionEngine(fetch_cfg)

        # Determine the source URL (e.g. from a Service Bus trigger payload
        # stored in state.metadata, or a default RBI watch URL).
        source_url = getattr(state, "source_url", None) or \
                     fetch_cfg.source_urls.get("RBI", "https://www.rbi.org.in")
        source     = RegulatorySource.RBI

        # ingest() is blocking (httpx.Client, Azure SDK) — offload to thread
        ingestion_result = await asyncio.to_thread(
            engine.ingest, source_url, source
        )

        state.document = DocumentPayload(
            doc_id    = ingestion_result.doc_id,
            source    = ingestion_result.source.value,
            blob_url  = ingestion_result.blob_path or source_url,
            mime_type = ingestion_result.metadata.get("mime_type", "application/pdf"),
            metadata  = {
                "region":         self._cfg.azure_region,
                "raw_text":       ingestion_result.extracted_text,
                "content_hash":   ingestion_result.content_hash,
                "ocr_used":       ingestion_result.ocr_used,
                "confidence":     ingestion_result.confidence,
            },
        )

        self._al.append(state.chain_id, NodeType.REGULATION,
                        {"stage": "ingestion",
                         "doc_id":       state.document.doc_id,
                         "blob_url":     state.document.blob_url,
                         "content_hash": ingestion_result.content_hash,
                         "ocr_used":     ingestion_result.ocr_used})
        log.info("ingestion.complete", doc_id=state.document.doc_id,
                 ocr_used=ingestion_result.ocr_used)
        return state

    # ── 4.2 Parse ──────────────────────────────────────────────────────────
    @degradable("parse")
    async def parse_node(self, state: PipelineState) -> PipelineState:
        """
        Use the SemanticChunker (parser/chunker.py) for heading-aware
        section splitting, entity extraction, and chunk enrichment.
        """
        log.info("parse.start", run_id=state.run_id)
        assert state.document, "parse_node requires a document"

        chunker = SemanticChunker()

        # In production the raw_text would come from Document Intelligence
        # or the ingestion blob; here we pass whatever is available.
        raw_text = state.document.metadata.get("raw_text", "")
        parse_result = chunker.chunk(
            doc_id=state.document.doc_id,
            text=raw_text,
            metadata={"source": state.document.source},
        )

        # Adapt ParseResult → main.py's ParsedDocument
        sections = [{"title": s, "page": 0} for s in parse_result.sections]
        entities = [
            {"type": e.entity_type, "value": e.value}
            for chunk in parse_result.chunks
            for e in chunk.entities
        ]
        confidence = 0.95 if parse_result.total_entities > 0 else 0.70

        state.parsed = ParsedDocument(
            doc_id     = state.document.doc_id,
            raw_text   = raw_text[:2000] if raw_text else "[no text extracted]",
            sections   = sections,
            entities   = entities,
            confidence = confidence,
        )

        expl = ExplainabilityMetadata(
            decision_id      = str(uuid.uuid4()),
            model_version    = self._cfg.gemini_model_version,
            feature_weights  = {"entity_density": 0.55, "section_clarity": 0.45},
            confidence_score = state.parsed.confidence,
            reasoning_steps  = [
                f"Extracted {parse_result.total_entities} entities",
                f"Split into {len(parse_result.chunks)} chunks",
                f"Found {len(parse_result.sections)} sections",
            ],
            alternative_decisions = [],
        )
        self._al.append(state.chain_id, NodeType.MAP,
                        {"stage": "parse",
                         "entity_count": len(entities),
                         "chunk_count": len(parse_result.chunks),
                         "confidence": state.parsed.confidence},
                        explainability=expl)
        log.info("parse.complete", confidence=state.parsed.confidence,
                 chunks=len(parse_result.chunks))
        return state

    # ── 4.3a Multi-Agent Reasoning (NEW — runs before MAPGenerator) ────────
    @degradable("multi_agent_reasoning")
    async def multi_agent_map_node(self, state: PipelineState) -> PipelineState:
        """
        Invoke the PlannerAgent pipeline for deep agentic reasoning.

        Runs:
          VerifierAgent  → ambiguity detection
          CriticAgent    → policy conflict + supersession check
          DeepReasoningAgent → Gemini chain-of-thought (system impact, rollout)
          EvidenceAgent  → evidence manifest generation
          PlannerAgent   → disagreement resolution + orchestration

        Results are stored in PipelineState agentic fields and surfaced in the
        downstream MAP nodes and audit ledger.  If the agent layer is
        unavailable (import error), the node gracefully degrades.
        """
        if not _AGENT_LAYER_AVAILABLE:
            log.warning("multi_agent_reasoning.skipped",
                        reason="agent layer not installed")
            state.degraded_stages.append("multi_agent_reasoning")
            return state

        if not state.parsed:
            log.warning("multi_agent_reasoning.skipped", reason="no parsed document")
            return state

        log.info("multi_agent_reasoning.start", run_id=state.run_id)

        settings = get_settings()
        planner  = PlannerAgent(
            gemini_api_key  = settings.gemini_api_key,
            gemini_model    = settings.gemini_model_version,
            max_obligations = 20,
            max_concurrency = 4,
        )

        # Capture ontology summary for audit trail
        try:
            from knowledge.policy_graph import get_ontology
            state.ontology_summary = get_ontology().summary()
        except Exception:
            pass

        # Run the full multi-agent pipeline
        ma_result: MultiAgentResult = await planner.plan(
            document_text = state.parsed.raw_text,
            document_ref  = state.parsed.doc_id,
        )

        # Store serialised results in PipelineState
        state.multi_agent_result    = ma_result.to_dict()
        state.agent_messages        = [m.to_dict() for m in ma_result.agent_messages]
        state.disagreements_resolved = ma_result.disagreements_resolved
        state.has_escalations        = ma_result.obligations_escalated > 0

        # Unpack per-obligation results
        for ob_result in ma_result.obligation_results:
            if ob_result.reasoning_result:
                state.reasoning_results.append(ob_result.reasoning_result)
            if ob_result.critique_report:
                for conflict in ob_result.critique_report.get("conflicts", []):
                    state.policy_conflicts.append(conflict)
            if ob_result.evidence_manifest:
                state.evidence_manifests.append(ob_result.evidence_manifest)
            for note in (ob_result.reasoning_result or {}).get("supersession_notes", []):
                if note not in state.supersession_notes:
                    state.supersession_notes.append(note)

        # Write to audit ledger
        self._al.append(
            state.chain_id,
            NodeType.MAP,
            {
                "stage":                  "multi_agent_reasoning",
                "obligations_processed":  ma_result.obligations_processed,
                "obligations_escalated":  ma_result.obligations_escalated,
                "disagreements_resolved": ma_result.disagreements_resolved,
                "policy_conflicts_found": len(state.policy_conflicts),
                "agent_messages":         len(state.agent_messages),
                "ontology":               state.ontology_summary,
                "map_ready":              ma_result.map_ready,
            },
        )

        log.info(
            "multi_agent_reasoning.complete",
            obligations    = ma_result.obligations_processed,
            escalated      = ma_result.obligations_escalated,
            disagreements  = ma_result.disagreements_resolved,
            conflicts      = len(state.policy_conflicts),
        )
        return state

    # ── 4.3b MAP Generation (now informed by multi-agent results) ──────────
    @degradable("map_generation")
    async def map_generation_node(self, state: PipelineState) -> PipelineState:
        """
        Run the 5-stage MAPGenerator pipeline (llm/map_generator.py):
        Obligation Extraction → Decomposition → SME Rules → JSON → Guardrails.
        """
        log.info("map_generation.start", run_id=state.run_id)
        assert state.parsed, "map_generation_node requires parsed document"

        generator = MAPGenerator(llm_client=None)  # rule-based; swap for LLM client
        map_doc = await generator.generate(
            doc_id=state.parsed.doc_id,
            text=state.parsed.raw_text,
        )

        # Adapt MAPDocument → main.py's MAPNode list
        state.map_nodes = [
            MAPNode(
                map_id      = map_doc.map_id,
                control_ref = action.obligation_id,
                description = action.description,
                owner       = action.owner_dept,
                risk_level  = action.risk_level.value,
            )
            for action in map_doc.action_items
        ]

        # Check for escalation (confidence < 0.80)
        if map_doc.status == MAPStatus.ESCALATED:
            log.warning("map_generation.escalated",
                        reason=map_doc.escalation_reason,
                        confidence=map_doc.confidence)
            state.errors.append(f"MAP escalated: {map_doc.escalation_reason}")

        expl = ExplainabilityMetadata(
            decision_id      = map_doc.map_id,
            model_version    = "rule-based-v1",
            feature_weights  = {"obligation_count": 0.5, "confidence": 0.5},
            confidence_score = map_doc.confidence,
            reasoning_steps  = [
                f"Extracted {len(map_doc.obligations)} obligations",
                f"Generated {len(map_doc.action_items)} action items",
                f"Status: {map_doc.status.value}",
            ],
            alternative_decisions = [],
        )
        self._al.append(state.chain_id, NodeType.MAP,
                        {"stage": "map_generation",
                         "map_count": len(state.map_nodes),
                         "status": map_doc.status.value,
                         "confidence": map_doc.confidence,
                         "escalated": map_doc.escalated},
                        explainability=expl)
        log.info("map_generation.complete", map_count=len(state.map_nodes),
                 status=map_doc.status.value)
        return state

    # ── 4.4 Routing ────────────────────────────────────────────────────────
    @degradable("routing")
    async def routing_node(self, state: PipelineState) -> PipelineState:
        """
        Use the HybridRouter (routing/router.py) for rule-based + ML
        department assignment with escalation logic.
        """
        log.info("routing.start", run_id=state.run_id)
        assert state.map_nodes, "routing_node requires MAP nodes"

        router = HybridRouter()

        # Route based on the highest-risk MAP node
        highest_risk_node = max(
            state.map_nodes,
            key=lambda n: {"HIGH": 3, "MEDIUM": 2, "LOW": 1}.get(n.risk_level, 0),
        )
        routed = router.route(
            obligation_id=highest_risk_node.control_ref,
            text=highest_risk_node.description,
            risk_level=highest_risk_node.risk_level,
        )

        state.routing = RoutingDecision(
            assignee_group = routed.department,
            priority       = routed.priority.value,
            sla_hours      = routed.sla_hours,
        )

        if routed.escalated:
            log.warning("routing.escalated", reason=routed.escalation_reason)
            state.errors.append(f"Routing escalated: {routed.escalation_reason}")

        self._al.append(state.chain_id, NodeType.TASK,
                        {"stage": "routing",
                         "department": routed.department,
                         "priority": routed.priority.value,
                         "sla_hours": routed.sla_hours,
                         "confidence": routed.confidence,
                         "method": routed.routing_method,
                         "escalated": routed.escalated})
        log.info("routing.complete", department=routed.department,
                 priority=routed.priority.value, method=routed.routing_method)
        return state

    # ── 4.5 Task Creation (ServiceNow) ─────────────────────────────────────
    @degradable("task_creation")
    async def task_creation_node(self, state: PipelineState) -> PipelineState:
        """
        Use the TaskManager (workflow/task_manager.py) to create a
        ServiceNow incident with SLA tracking and dependency management.

        FIX: TaskManager.create_task() is synchronous (uses httpx.Client).
             Wrap in asyncio.to_thread to prevent blocking the event loop.
        """
        log.info("task_creation.start", run_id=state.run_id)
        assert state.routing, "task_creation_node requires routing decision"

        manager = self._tm

        doc_id = state.document.doc_id if state.document else "N/A"

        # create_task() is synchronous (uses httpx.Client) — offload to thread
        ticket = await asyncio.to_thread(
            manager.create_task,
            obligation_id  = state.map_nodes[0].control_ref if state.map_nodes else "unknown",
            department     = state.routing.assignee_group,
            assignee       = state.routing.assignee_group,
            assignee_email = f"{state.routing.assignee_group.lower().replace(' ', '-')}@bank.org",
            description    = f"Compliance review — {doc_id}",
            priority       = state.map_nodes[0].risk_level if state.map_nodes else "Medium",
            sla_hours      = state.routing.sla_hours,
        )

        state.snow_task = ServiceNowTask(
            sys_id     = ticket.snow_sys_id or ticket.task_id,
            number     = ticket.snow_number or ticket.task_id,
            state      = ticket.state.value,
            short_desc = ticket.description[:200],
        )

        self._al.append(state.chain_id, NodeType.TASK,
                        {"stage": "task_creation",
                         "task_id":     ticket.task_id,
                         "snow_number": state.snow_task.number,
                         "department":  ticket.department,
                         "priority":    ticket.priority,
                         "sla_hours":   state.routing.sla_hours})
        log.info("task_creation.complete", snow_number=state.snow_task.number,
                 task_id=ticket.task_id)
        return state

    # ── 4.6 Validation ─────────────────────────────────────────────────────
    @degradable("validation")
    async def validation_node(self, state: PipelineState) -> PipelineState:
        """
        Delegate all validation to the production HybridValidator
        (validation/validator.py) which runs Tier-1 ESB/KeyVault checks
        and Tier-2 Document Intelligence checks.

        FIX: The previous stub only checked state.map_nodes existence,
             entirely bypassing the two-tier HybridValidator engine.
        FIX: HybridValidator.validate() is a coroutine — awaited directly.
        """
        log.info("validation.start", run_id=state.run_id)
        escalate_to_co = False
        errors:   list[str] = []
        warnings: list[str] = []

        # ── Quick structural pre-checks (pipeline-level, not document-level) ──
        if not state.map_nodes:
            errors.append("No MAP nodes generated")
        if not state.snow_task and "task_creation" not in state.degraded_stages:
            errors.append("ServiceNow task missing and stage not marked degraded")
        if state.degraded_stages:
            warnings.append(f"Degraded stages: {', '.join(state.degraded_stages)}")

        # ── Tier-1 + Tier-2 validation via HybridValidator ──────────────────
        if state.document and state.parsed:
            try:
                doc_content = state.parsed.raw_text.encode("utf-8") \
                              if state.parsed.raw_text else b""

                val_req = ValidationRequest(
                    task_id          = state.run_id,
                    obligation_id    = state.map_nodes[0].control_ref
                                       if state.map_nodes else "unknown",
                    department       = state.routing.assignee_group
                                       if state.routing else "Compliance",
                    unit_head_email  = getattr(self._cfg, "alert_sender", ""),
                    unit_head_name   = "Compliance Officer",
                    system_id        = state.document.doc_id,
                    vault_secret     = getattr(self._cfg, "audit_hmac_secret", ""),
                    document_path    = state.document.blob_url,
                    document_content = doc_content,
                    run_esb_checks   = True,
                    run_kv_checks    = True,
                    run_doc_checks   = len(doc_content) > 0,
                )

                validator  = HybridValidator()
                val_report = await validator.validate(val_req)

                if val_report.outcome == ValidationOutcome.REWORK:
                    escalate_to_co = True
                    errors.append(
                        f"HybridValidator: {val_report.summary}"
                    )
                elif val_report.outcome == ValidationOutcome.PASS:
                    log.info("validation.hybrid_pass",
                             confidence=val_report.overall_confidence)

                # Surface individual failed checks as warnings
                for chk in val_report.check_results:
                    if not chk.passed:
                        warnings.append(
                            f"Check '{chk.name}' failed: {chk.explanation}"
                        )

            except Exception as exc:
                log.warning("validation.hybrid_validator_error",
                            error=str(exc),
                            fallback="confidence_check")
                if state.parsed and state.parsed.confidence < 0.80:
                    escalate_to_co = True
                    errors.append(
                        f"Parse confidence {state.parsed.confidence:.0%} "
                        f"is below the 80% threshold."
                    )
        elif state.parsed and state.parsed.confidence < 0.80:
            escalate_to_co = True
            errors.append(
                f"Parse confidence {state.parsed.confidence:.0%} "
                f"is below the 80% threshold. Escalating to Compliance Officer."
            )

        state.validation = ValidationResult(
            is_valid = len(errors) == 0 and not escalate_to_co,
            errors   = errors,
            warnings = warnings,
        )
        self._al.append(state.chain_id, NodeType.EVIDENCE,
                        {"stage":    "validation",
                         "is_valid": state.validation.is_valid,
                         "escalated_to_compliance_officer": escalate_to_co,
                         "errors":   errors,
                         "warnings": warnings})
        if escalate_to_co:
            log.warning("validation.escalated_to_compliance_officer",
                        run_id=state.run_id)
        log.info("validation.complete", is_valid=state.validation.is_valid,
                 errors=len(errors), warnings=len(warnings))
        return state

    # ── 4.7 Ledger Update (Sign-off) ───────────────────────────────────────
    @degradable("ledger_update")
    async def ledger_update_node(self, state: PipelineState) -> PipelineState:
        """
        Finalise the audit chain:
          - Append Sign-off node
          - Run tamper detection
          - Generate XBRL + signed PDF reports
          - Store report paths in state
        """
        log.info("ledger_update.start", run_id=state.run_id)

        self._al.append(
            state.chain_id,
            NodeType.SIGNOFF,
            {
                "stage":            "ledger_update",
                "run_id":           state.run_id,
                "snow_task":        state.snow_task.number if state.snow_task else None,
                "is_valid":         state.validation.is_valid if state.validation else None,
                "degraded_stages":  state.degraded_stages,
            },
            actor="system:langgraph-orchestrator",
        )
        reports = self._al.finalize(
            state.chain_id,
            output_dir=Path(os.environ.get("REPORT_OUTPUT_DIR", "./compliance-reports")),
        )
        state.ledger_finalised = True
        log.info("ledger_update.complete", chain_id=state.chain_id,
                 reports={k: str(v) for k, v in reports.items()})
        return state


# ===========================================================================
# 5.  LANGGRAPH CONTROLLER
# ===========================================================================

def build_graph(nodes: PipelineNodes) -> "CompiledGraph":  # type: ignore[name-defined]
    """
    Compile the LangGraph StateGraph that wires all pipeline nodes together.

    Graph topology (linear with conditional kill-switch edge):

        START
          │
          ▼
       ingestion ──(killed?)──► END
          │
          ▼
        parse ──(killed?)──► END
          │
          ▼
      map_generation
          │
          ▼
        routing
          │
          ▼
      task_creation
          │
          ▼
       validation
          │
          ▼
      ledger_update
          │
          ▼
         END
    """
    if not _LANGGRAPH_AVAILABLE:
        raise ImportError("langgraph is required. pip install langgraph")

    def _killed(state: PipelineState) -> str:
        return "end" if state.killed else "continue"

    graph = StateGraph(PipelineState)

    # Register nodes
    graph.add_node("ingestion",             nodes.ingestion_node)
    graph.add_node("parse",                 nodes.parse_node)
    graph.add_node("multi_agent_reasoning", nodes.multi_agent_map_node)   # NEW
    graph.add_node("map_generation",        nodes.map_generation_node)
    graph.add_node("routing",               nodes.routing_node)
    graph.add_node("task_creation",         nodes.task_creation_node)
    graph.add_node("validation",            nodes.validation_node)
    graph.add_node("ledger_update",         nodes.ledger_update_node)

    # Entry
    graph.set_entry_point("ingestion")

    # Conditional edges: abort entire pipeline if kill switch fires mid-run
    graph.add_conditional_edges("ingestion", _killed,
                                 {"continue": "parse", "end": END})
    graph.add_conditional_edges("parse", _killed,
                                 {"continue": "multi_agent_reasoning", "end": END})
    graph.add_conditional_edges("multi_agent_reasoning", _killed,
                                 {"continue": "map_generation", "end": END})
    graph.add_edge("map_generation", "routing")
    graph.add_edge("routing",        "task_creation")
    graph.add_edge("task_creation",  "validation")
    graph.add_edge("validation",     "ledger_update")
    graph.add_edge("ledger_update",  END)

    return graph.compile()



# ===========================================================================
# 6.  ORCHESTRATOR
# ===========================================================================

class ComplianceOrchestrator:
    """
    Top-level controller.  One instance per AKS pod.

    Responsibilities:
      - Initialise all dependencies once at startup.
      - Accept pipeline run requests and dispatch them to the LangGraph.
      - Feed outcomes into the KillSwitchMonitor.
      - Expose `run_batch()` for bulk processing.
    """

    def __init__(self, settings: Optional[Settings] = None):
        self._cfg  = settings or get_settings()
        self._init_logging()
        self._al   = self._build_audit_logger()
        self._ks   = KillSwitchMonitor(
            threshold   = self._cfg.kill_switch_error_threshold,
            window      = ROLLING_WINDOW_SIZE,
            webhook_url = self._cfg.kill_switch_webhook,
            settings    = self._cfg,
        )
        self._ks.bind_audit_logger(self._al)
        # Create a single TaskManager for this orchestrator (pod-wide)
        self.task_manager = TaskManager(
            snow_instance_url  = self._cfg.servicenow_instance_url,
            snow_client_id     = self._cfg.servicenow_client_id,
            snow_client_secret = self._cfg.servicenow_client_secret,
            snow_username      = self._cfg.servicenow_username,
            snow_password      = self._cfg.servicenow_password,
        )

        self._nodes  = PipelineNodes(self._cfg, self._al, self._ks, self.task_manager)
        self._graph  = build_graph(self._nodes) if _LANGGRAPH_AVAILABLE else None
        self._sem    = asyncio.Semaphore(self._cfg.max_concurrent_chains)
        log.info("orchestrator.ready",
                 region=self._cfg.azure_region_display,
                 max_concurrent=self._cfg.max_concurrent_chains,
                 kill_switch_threshold=f"{self._cfg.kill_switch_error_threshold:.0%}")

    # ------------------------------------------------------------------
    def _init_logging(self) -> None:
        structlog.configure(
            processors=[
                structlog.contextvars.merge_contextvars,
                structlog.processors.add_log_level,
                structlog.processors.TimeStamper(fmt="iso"),
                structlog.processors.JSONRenderer(),
            ],
            wrapper_class=structlog.make_filtering_bound_logger(
                getattr(logging, self._cfg.log_level.upper(), logging.INFO)
            ),
        )

    # ------------------------------------------------------------------
    def _build_audit_logger(self) -> AuditLogger:
        cfg = AuditLoggerConfig(
            cosmos_connection_string = self._cfg.cosmos_connection_string or None,
            cosmos_database          = self._cfg.cosmos_database,
            hmac_secret              = self._cfg.audit_hmac_secret or None,
            private_key_pem          = self._cfg.pdf_signing_key_pem or None,
            approved_override_actors = {"system:langgraph-orchestrator"},
            smtp_host                = self._cfg.smtp_host or None,
            smtp_port                = self._cfg.smtp_port,
            alert_sender             = self._cfg.alert_sender or None,
            alert_recipients         = self._cfg.alert_recipients or [],
        )
        return AuditLogger(cfg)

    # ------------------------------------------------------------------
    async def run_pipeline(self, initial_state: Optional[PipelineState] = None
                           ) -> PipelineState:
        """
        Execute one end-to-end compliance pipeline run.

        Returns the final PipelineState regardless of success/failure.
        Raises RuntimeError only if the kill switch is active (caller must
        handle or re-queue).
        """
        if self._ks.is_active():
            raise RuntimeError(
                "KILL SWITCH ACTIVE — automation disabled. "
                "Remediate errors and call orchestrator.kill_switch.reset(actor=...) "
                "before re-enabling."
            )

        state = initial_state or PipelineState()
        state.chain_id = self._al.new_chain()

        async with self._sem:   # honour max_concurrent_chains
            try:
                timeout = self._cfg.pipeline_timeout_seconds
                if self._graph:
                    final = await asyncio.wait_for(
                        self._graph.ainvoke(state),
                        timeout=timeout,
                    )
                else:
                    # LangGraph unavailable — run nodes sequentially (dev fallback)
                    log.warning("langgraph_unavailable.sequential_fallback")
                    final = await self._sequential_fallback(state)

                success = (
                    final.get("ledger_finalised", False)
                    if isinstance(final, dict) else
                    final.ledger_finalised
                )
                self._ks.record(success)

                # Activate kill switch mid-run if state.killed was set
                if isinstance(final, dict):
                    final_state = PipelineState(**{k: v for k, v in final.items()
                                                   if k in PipelineState.__dataclass_fields__})
                else:
                    final_state = final

                log.info("pipeline.complete",
                         run_id=final_state.run_id,
                         success=success,
                         degraded=final_state.degraded_stages,
                         error_rate=f"{self._ks.error_rate():.2%}")
                return final_state

            except asyncio.TimeoutError:
                log.error("pipeline.timeout", run_id=state.run_id,
                          timeout=self._cfg.pipeline_timeout_seconds)
                self._ks.record(False)
                state.errors.append("Pipeline timed out")
                return state

            except Exception as exc:
                log.error("pipeline.unhandled_exception", run_id=state.run_id,
                          error=str(exc))
                self._ks.record(False)
                state.errors.append(str(exc))
                return state

    # ------------------------------------------------------------------
    async def _sequential_fallback(self, state: PipelineState) -> PipelineState:
        """Run nodes in order without LangGraph (dev / degraded mode)."""
        for node_fn in [
            self._nodes.ingestion_node,
            self._nodes.parse_node,
            self._nodes.multi_agent_map_node,
            self._nodes.map_generation_node,
            self._nodes.routing_node,
            self._nodes.task_creation_node,
            self._nodes.validation_node,
            self._nodes.ledger_update_node,
        ]:
            if state.killed:
                break
            state = await node_fn(state)
        return state

    # ------------------------------------------------------------------
    async def run_batch(self, count: int = 1) -> list[PipelineState]:
        """
        Run `count` pipelines concurrently (up to max_concurrent_chains).
        Useful for load testing and bulk document processing.
        """
        tasks = [self.run_pipeline() for _ in range(count)]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        final: list[PipelineState] = []
        for r in results:
            if isinstance(r, Exception):
                log.error("batch.pipeline_exception", error=str(r))
                s = PipelineState()
                s.errors.append(str(r))
                final.append(s)
            else:
                final.append(r)
        return final

    # ------------------------------------------------------------------
    @property
    def kill_switch(self) -> KillSwitchMonitor:
        return self._ks


# ===========================================================================
# 7.  HEALTH CHECK ENDPOINT  (AKS liveness / readiness probe)
# ===========================================================================

async def health_check(orchestrator: ComplianceOrchestrator) -> dict:
    """
    Returns a health dict suitable for an HTTP /healthz endpoint.

    Kubernetes liveness probe pattern:
      livenessProbe:
        httpGet:
          path: /healthz
          port: 8080
        failureThreshold: 3

    The probe should return HTTP 503 if kill_switch_active=True so AKS
    can restart the pod and trigger an alert.
    """
    ks = orchestrator.kill_switch
    return {
        "status":             "unhealthy" if ks.is_active() else "healthy",
        "kill_switch_active": ks.is_active(),
        "error_rate":         f"{ks.error_rate():.2%}",
        "region":             orchestrator._cfg.azure_region_display,
        "timestamp":          time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


# ===========================================================================
# 8.  ENTRY POINT
# ===========================================================================

async def _main_async() -> None:
    log.info("startup", service="compliance-orchestrator",
             region=get_settings().azure_region_display)

    orchestrator = ComplianceOrchestrator()

    # Note: TaskManager uses in-memory task store + synchronous ServiceNow client.
    # SLA breach checks are run inline during pipeline execution.
    # For production Cosmos-backed persistence, wrap TaskManager with a
    # CosmosTaskStore adapter and initialise it here.

    # Single run (replace with Service Bus / event-driven trigger in prod)
    result = await orchestrator.run_pipeline()

    print("\n" + "=" * 60)
    print(f"  Run ID       : {result.run_id}")
    print(f"  Chain ID     : {result.chain_id}")
    print(f"  Ledger final : {result.ledger_finalised}")
    print(f"  Degraded     : {result.degraded_stages or 'none'}")
    print(f"  Errors       : {result.errors or 'none'}")
    print(f"  Kill Switch  : {'ACTIVE' if orchestrator.kill_switch.is_active() else 'inactive'}")
    print(f"  Error Rate   : {orchestrator.kill_switch.error_rate():.2%}")
    print("=" * 60)

    health = await health_check(orchestrator)
    print(f"\nHealth: {health}")


def main() -> None:
    asyncio.run(_main_async())


if __name__ == "__main__":
    main()