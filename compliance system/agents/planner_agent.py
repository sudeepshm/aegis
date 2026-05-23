"""
agents/planner_agent.py
========================
PlannerAgent — Orchestrates the multi-agent pipeline with disagreement resolution.

Pipeline:
  1. Decompose document text into individual obligations
  2. For each obligation:
     a. VerifierAgent  → AmbiguityReport
     b. CriticAgent    → CritiqueReport
     c. DeepReasoningAgent → ReasoningResult
     d. EvidenceAgent  → EvidenceManifest
  3. Disagreement resolution:
     - If VerifierAgent flags HIGH ambiguity AND CriticAgent flags no conflicts:
       trigger 3rd-pass EvidenceAgent deep scan
     - If CriticAgent flags HIGH conflict: escalate, do not generate MAP node
  4. Assemble final MultiAgentResult

All inter-agent messages are recorded in AgentMessage log for audit trail.
"""
from __future__ import annotations

import asyncio
import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from agents.verifier_agent import VerifierAgent, AmbiguityReport
from agents.critic_agent import CriticAgent, CritiqueReport
from agents.evidence_agent import EvidenceAgent
from agents.reasoning_agent import DeepReasoningAgent

logger = logging.getLogger(__name__)


# =============================================================================
# Data Structures
# =============================================================================

@dataclass
class AgentMessage:
    """
    Typed inter-agent message for audit trail.
    Every interaction between agents is logged as an AgentMessage.
    """
    message_id:   str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    sender:       str = ""
    receiver:     str = ""
    message_type: str = ""   # REQUEST | RESPONSE | DISAGREEMENT | RESOLUTION | ESCALATION
    payload_ref:  str = ""   # reference to the obligation or result
    content:      str = ""
    timestamp:    str = field(
        default_factory=lambda: datetime.now(tz=timezone.utc).isoformat()
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "message_id":   self.message_id,
            "sender":       self.sender,
            "receiver":     self.receiver,
            "message_type": self.message_type,
            "payload_ref":  self.payload_ref,
            "content":      self.content,
            "timestamp":    self.timestamp,
        }


@dataclass
class ObligationResult:
    """Result of processing a single obligation through all agents."""
    obligation_text:   str
    obligation_index:  int
    ambiguity_report:  Optional[dict[str, Any]] = None
    critique_report:   Optional[dict[str, Any]] = None
    reasoning_result:  Optional[dict[str, Any]] = None
    evidence_manifest: Optional[dict[str, Any]] = None
    escalated:         bool = False
    escalation_reason: str = ""
    disagreements_resolved: int = 0
    processing_notes:  list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "obligation_text":       self.obligation_text[:200],
            "obligation_index":      self.obligation_index,
            "ambiguity_report":      self.ambiguity_report,
            "critique_report":       self.critique_report,
            "reasoning_result":      self.reasoning_result,
            "evidence_manifest":     self.evidence_manifest,
            "escalated":             self.escalated,
            "escalation_reason":     self.escalation_reason,
            "disagreements_resolved": self.disagreements_resolved,
            "processing_notes":      self.processing_notes,
        }


@dataclass
class MultiAgentResult:
    """Final output of the multi-agent pipeline for a full document."""
    run_id:               str = field(default_factory=lambda: str(uuid.uuid4()))
    document_ref:         str = ""
    obligations_processed: int = 0
    obligations_escalated: int = 0
    disagreements_resolved: int = 0
    obligation_results:   list[ObligationResult] = field(default_factory=list)
    agent_messages:       list[AgentMessage] = field(default_factory=list)
    map_ready:            bool = False
    pipeline_summary:     str = ""
    generated_at:         str = field(
        default_factory=lambda: datetime.now(tz=timezone.utc).isoformat()
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id":                self.run_id,
            "document_ref":          self.document_ref,
            "obligations_processed": self.obligations_processed,
            "obligations_escalated": self.obligations_escalated,
            "disagreements_resolved": self.disagreements_resolved,
            "map_ready":             self.map_ready,
            "pipeline_summary":      self.pipeline_summary,
            "generated_at":          self.generated_at,
            "obligation_results":    [r.to_dict() for r in self.obligation_results],
            "agent_message_log":     [m.to_dict() for m in self.agent_messages],
        }


# =============================================================================
# Disagreement Resolution Rules
# =============================================================================

def _resolve_disagreement(
    ambiguity: AmbiguityReport,
    critique: CritiqueReport,
) -> tuple[str, bool]:
    """
    Apply disagreement resolution logic.

    Returns (resolution_note, needs_3rd_pass).

    Resolution rules:
      R1: HIGH ambiguity + no conflicts → 3rd-pass evidence deep-scan required
      R2: HIGH ambiguity + HIGH conflict → escalate to compliance officer
      R3: LOW ambiguity + HIGH conflict → block MAP generation, require legal review
      R4: HIGH ambiguity + LOW conflict → flag for legal clarification, proceed cautiously
      R5: All clear → proceed normally
    """
    high_ambiguity = ambiguity.ambiguity_score > 0.45
    high_conflict  = not critique.critique_passed  # critique_passed=False means HIGH conflict

    if high_ambiguity and high_conflict:
        return (
            "DISAGREEMENT R2: HIGH ambiguity AND HIGH conflict detected. "
            "Escalating to Compliance Officer for manual review.",
            False,
        )
    elif high_ambiguity and not high_conflict:
        return (
            "DISAGREEMENT R1: VerifierAgent flagged HIGH ambiguity but CriticAgent found no conflicts. "
            "Triggering 3rd-pass EvidenceAgent deep scan to determine if ambiguity creates compliance risk.",
            True,   # needs_3rd_pass
        )
    elif not high_ambiguity and high_conflict:
        return (
            "DISAGREEMENT R3: LOW ambiguity but HIGH conflict. "
            "Blocking MAP generation — legal review required to resolve conflict.",
            False,
        )
    elif high_ambiguity:
        return (
            "DISAGREEMENT R4: Moderate ambiguity, no major conflicts. "
            "Flagging for legal clarification. Proceeding with caution.",
            False,
        )
    return ("No disagreement — all agents aligned.", False)


# =============================================================================
# Planner Agent
# =============================================================================

class PlannerAgent:
    """
    Orchestrates the complete multi-agent compliance pipeline.

    Usage::

        planner = PlannerAgent(gemini_api_key="...")
        result = await planner.plan(
            document_text="Banks shall encrypt...",
            document_ref="RBI/2023-24/101",
        )
        print(result.obligations_processed, result.disagreements_resolved)
    """

    # Obligation extraction pattern
    _SENTENCE_SPLITTER = re.compile(
        r'(?<=[.;])\s+(?=[A-Z])'
        r'|(?<=\n)\s*(?:\d+\.|[•\-])\s+',
    )

    def __init__(
        self,
        gemini_api_key:   str = "",
        gemini_model:     str = "gemini-1.5-pro",
        max_obligations:  int = 20,
        max_concurrency:  int = 4,
    ):
        self._verifier  = VerifierAgent()
        self._critic    = CriticAgent()
        self._evidence  = EvidenceAgent()
        self._reasoner  = DeepReasoningAgent(
            gemini_api_key = gemini_api_key,
            gemini_model   = gemini_model,
        )
        self._max_obs   = max_obligations
        self._semaphore = asyncio.Semaphore(max_concurrency)

    async def plan(
        self,
        document_text: str,
        document_ref:  str = "",
        domain:        str = "",
    ) -> MultiAgentResult:
        """
        Entry point: process a full regulatory document.

        1. Extract obligations from document text
        2. Process each obligation through the agent pipeline (with concurrency limit)
        3. Apply disagreement resolution
        4. Assemble MultiAgentResult
        """
        result = MultiAgentResult(document_ref=document_ref)

        # Step 1: Obligation extraction
        obligations = self._extract_obligations(document_text)
        if not obligations:
            result.pipeline_summary = "No obligations extracted from document."
            return result

        self._log(result, "PlannerAgent", "Pipeline",
                  "REQUEST", document_ref,
                  f"Planning {len(obligations)} obligations from '{document_ref}'")

        # Step 2: Parallel processing with semaphore
        tasks = [
            self._process_obligation(result, ob, idx, domain, document_ref)
            for idx, ob in enumerate(obligations)
        ]
        obligation_results = await asyncio.gather(*tasks, return_exceptions=True)

        # Filter exceptions
        for i, res in enumerate(obligation_results):
            if isinstance(res, Exception):
                logger.error("Obligation %d failed: %s", i, res)
                fallback = ObligationResult(
                    obligation_text  = obligations[i],
                    obligation_index = i,
                    escalated        = True,
                    escalation_reason = f"Processing error: {res}",
                )
                result.obligation_results.append(fallback)
            else:
                result.obligation_results.append(res)

        # Step 3: Aggregate stats
        result.obligations_processed = len(result.obligation_results)
        result.obligations_escalated = sum(
            1 for r in result.obligation_results if r.escalated
        )
        result.disagreements_resolved = sum(
            r.disagreements_resolved for r in result.obligation_results
        )
        result.map_ready = result.obligations_escalated == 0

        # Step 4: Summary
        result.pipeline_summary = (
            f"Processed {result.obligations_processed} obligation(s). "
            f"Escalated: {result.obligations_escalated}. "
            f"Disagreements resolved: {result.disagreements_resolved}. "
            f"MAP ready: {result.map_ready}."
        )

        self._log(result, "PlannerAgent", "AuditLogger",
                  "RESPONSE", document_ref, result.pipeline_summary)

        logger.info("PlannerAgent complete: %s", result.pipeline_summary)
        return result

    async def _process_obligation(
        self,
        result:       MultiAgentResult,
        obligation:   str,
        idx:          int,
        domain:       str,
        document_ref: str,
    ) -> ObligationResult:
        """Process a single obligation through all agents."""
        async with self._semaphore:
            ob_result = ObligationResult(
                obligation_text  = obligation,
                obligation_index = idx,
            )

            ref = f"OB-{idx:02d}"
            self._log(result, "PlannerAgent", "VerifierAgent",
                      "REQUEST", ref, obligation[:80])

            # Agent A: Verifier
            ambiguity = self._verifier.verify(obligation)
            ob_result.ambiguity_report = ambiguity.to_dict()
            self._log(result, "VerifierAgent", "PlannerAgent",
                      "RESPONSE", ref,
                      f"Ambiguity score: {ambiguity.ambiguity_score:.2f}, "
                      f"flags: {len(ambiguity.flags)}")

            # Agent B: Critic
            self._log(result, "PlannerAgent", "CriticAgent",
                      "REQUEST", ref, obligation[:80])
            critique = self._critic.critique(
                obligation_text = obligation,
                regulation_ref  = document_ref,
                domain          = domain,
            )
            ob_result.critique_report = critique.to_dict()
            self._log(result, "CriticAgent", "PlannerAgent",
                      "RESPONSE", ref,
                      f"Conflicts: {len(critique.conflicts)}, passed: {critique.critique_passed}")

            # Disagreement resolution
            resolution_note, needs_3rd_pass = _resolve_disagreement(ambiguity, critique)
            if resolution_note != "No disagreement — all agents aligned.":
                ob_result.disagreements_resolved += 1
                ob_result.processing_notes.append(resolution_note)
                self._log(result, "PlannerAgent", "PlannerAgent",
                          "DISAGREEMENT", ref, resolution_note)

                # Escalation check: R2 or R3
                if "Escalating" in resolution_note or "Blocking" in resolution_note:
                    ob_result.escalated = True
                    ob_result.escalation_reason = resolution_note
                    self._log(result, "PlannerAgent", "ComplianceOfficer",
                              "ESCALATION", ref, resolution_note)
                    return ob_result

                # 3rd pass for R1
                if needs_3rd_pass:
                    self._log(result, "PlannerAgent", "EvidenceAgent",
                              "REQUEST", ref, "3rd-pass deep scan triggered")
                    deep_manifest = self._evidence.infer_evidence(
                        obligation_text = obligation,
                        domain          = domain,
                    )
                    ob_result.evidence_manifest = deep_manifest.to_dict()
                    ob_result.disagreements_resolved += 1
                    ob_result.processing_notes.append(
                        f"3rd-pass EvidenceAgent scan completed: "
                        f"{len(deep_manifest.required_evidence)} evidence items identified."
                    )
                    self._log(result, "EvidenceAgent", "PlannerAgent",
                              "RESOLUTION", ref,
                              f"Deep scan: {deep_manifest.completeness_pct:.0f}% auto-collectible")

            # Agent C: Deep Reasoner
            self._log(result, "PlannerAgent", "DeepReasoningAgent",
                      "REQUEST", ref, obligation[:80])
            reasoning = await self._reasoner.reason(
                obligation_text = obligation,
                regulation_ref  = document_ref,
                domain          = domain,
            )
            ob_result.reasoning_result = reasoning.to_dict()
            self._log(result, "DeepReasoningAgent", "PlannerAgent",
                      "RESPONSE", ref,
                      f"Risk: {reasoning.operational_risk}, "
                      f"systems: {len(reasoning.impacted_systems)}, "
                      f"confidence: {reasoning.confidence:.2f}")

            # Agent D: Evidence (if not already done in 3rd pass)
            if ob_result.evidence_manifest is None:
                self._log(result, "PlannerAgent", "EvidenceAgent",
                          "REQUEST", ref, obligation[:80])
                # Phase 9: rollout_sequence is now list[dict] — extract 'task' strings
                action_items_str: list[str] = [
                    n["task"] if isinstance(n, dict) else str(n)
                    for n in reasoning.rollout_sequence
                ]
                manifest = self._evidence.infer_evidence(
                    obligation_text = obligation,
                    action_items    = action_items_str,
                    domain          = domain,
                )
                ob_result.evidence_manifest = manifest.to_dict()
                self._log(result, "EvidenceAgent", "PlannerAgent",
                          "RESPONSE", ref,
                          f"Evidence: {len(manifest.required_evidence)} items, "
                          f"{manifest.completeness_pct:.0f}% auto-collectible")

            ob_result.processing_notes.append(
                f"Completed: risk={reasoning.operational_risk}, "
                f"conflicts={len(critique.conflicts)}, "
                f"evidence_items={len(ob_result.evidence_manifest.get('evidence_items', []))}"
            )
            return ob_result

    # ── Obligation Extraction ─────────────────────────────────────────────────

    def _extract_obligations(self, document_text: str) -> list[str]:
        """
        Extract individual obligation sentences from a regulatory document.
        Caps at self._max_obs to prevent runaway processing.
        """
        # Split on sentences
        sentences = self._SENTENCE_SPLITTER.split(document_text)

        # Filter: must be substantive (>30 chars) and contain obligation keywords
        obligation_pattern = re.compile(
            r'\b(?:shall|must|required|mandatory|directed|should|obligated|'
            r'encrypt|report|retain|implement|maintain|conduct|notify|'
            r'screen|verify|monitor|backup|patch|erase)\b',
            re.I,
        )
        obligations = [
            s.strip()
            for s in sentences
            if len(s.strip()) > 30 and obligation_pattern.search(s)
        ]

        # Deduplicate and cap
        seen: set[str] = set()
        unique: list[str] = []
        for ob in obligations:
            key = ob[:80].lower()
            if key not in seen:
                seen.add(key)
                unique.append(ob)
        return unique[:self._max_obs]

    # ── Message Logging ───────────────────────────────────────────────────────

    @staticmethod
    def _log(
        result:       MultiAgentResult,
        sender:       str,
        receiver:     str,
        msg_type:     str,
        payload_ref:  str,
        content:      str,
    ) -> None:
        msg = AgentMessage(
            sender       = sender,
            receiver     = receiver,
            message_type = msg_type,
            payload_ref  = payload_ref,
            content      = content[:200],
        )
        result.agent_messages.append(msg)
        logger.debug(
            "[%s] %s → %s (%s): %s",
            msg_type, sender, receiver, payload_ref, content[:80],
        )
