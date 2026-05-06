"""
llm/map_generator.py
====================
5-Stage LangGraph workflow for Measurable Action Point (MAP) generation.

Stages
------
  1. Obligation Extraction   → identify regulatory obligations from parsed text
  2. Decomposition           → break obligations into atomic action items
  3. SME Rules Application   → apply subject-matter-expert business rules
  4. JSON Structuring        → produce structured MAP JSON output
  5. Guardrails Validation   → confidence checks, schema validation, escalation

Dependencies
------------
  pip install langchain-core langchain-google-genai langgraph
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

logger = logging.getLogger(__name__)

CONFIDENCE_THRESHOLD = 0.80  # below this → escalate to Compliance Officer
MAX_RETRIES = 2


# ──────────────────────────────────────────────────────────────────────────────
# Data Structures
# ──────────────────────────────────────────────────────────────────────────────

class RiskLevel(str, Enum):
    HIGH   = "HIGH"
    MEDIUM = "MEDIUM"
    LOW    = "LOW"


class MAPStatus(str, Enum):
    DRAFT     = "DRAFT"
    VALIDATED = "VALIDATED"
    ESCALATED = "ESCALATED"
    FAILED    = "FAILED"


@dataclass
class Obligation:
    """A single regulatory obligation extracted from source text."""
    obligation_id:   str
    source_text:     str
    regulation_ref:  str
    description:     str
    confidence:      float = 0.0
    keywords:        list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "obligation_id":  self.obligation_id,
            "regulation_ref": self.regulation_ref,
            "description":    self.description,
            "confidence":     round(self.confidence, 4),
            "keywords":       self.keywords,
        }


@dataclass
class ActionItem:
    """An atomic action decomposed from an obligation."""
    action_id:      str
    obligation_id:  str
    description:    str
    owner_dept:     str = ""
    risk_level:     RiskLevel = RiskLevel.MEDIUM
    deadline_days:  int = 30
    evidence_type:  str = ""
    dependencies:   list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "action_id":     self.action_id,
            "obligation_id": self.obligation_id,
            "description":   self.description,
            "owner_dept":    self.owner_dept,
            "risk_level":    self.risk_level.value,
            "deadline_days": self.deadline_days,
            "evidence_type": self.evidence_type,
            "dependencies":  self.dependencies,
        }


@dataclass
class MAPDocument:
    """Complete Measurable Action Point document."""
    map_id:        str
    doc_id:        str
    obligations:   list[Obligation] = field(default_factory=list)
    action_items:  list[ActionItem] = field(default_factory=list)
    status:        MAPStatus = MAPStatus.DRAFT
    confidence:    float = 0.0
    escalated:     bool = False
    escalation_reason: str = ""
    errors:        list[str] = field(default_factory=list)
    generated_at:  str = field(
        default_factory=lambda: datetime.now(tz=timezone.utc).isoformat()
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "map_id":            self.map_id,
            "doc_id":            self.doc_id,
            "status":            self.status.value,
            "confidence":        round(self.confidence, 4),
            "escalated":         self.escalated,
            "escalation_reason": self.escalation_reason,
            "obligation_count":  len(self.obligations),
            "action_item_count": len(self.action_items),
            "obligations":       [o.to_dict() for o in self.obligations],
            "action_items":      [a.to_dict() for a in self.action_items],
            "errors":            self.errors,
            "generated_at":      self.generated_at,
        }


# ──────────────────────────────────────────────────────────────────────────────
# SME Rules Engine
# ──────────────────────────────────────────────────────────────────────────────

class SMERulesEngine:
    """
    Subject-matter-expert rules for Indian regulatory compliance.
    Maps regulation types to departments, risk levels, and deadlines.
    """

    DEPARTMENT_MAP: dict[str, str] = {
        "kyc":         "Operations",
        "aml":         "Compliance",
        "cft":         "Compliance",
        "data protection": "IT Security",
        "encryption":  "IT Security",
        "capital adequacy": "Finance",
        "npa":         "Credit Risk",
        "liquidity":   "Treasury",
        "cyber":       "IT Security",
        "outsourcing": "Vendor Management",
        "audit":       "Internal Audit",
        "grievance":   "Customer Service",
        "fraud":       "Fraud Management",
        "it governance": "IT Governance",
    }

    RISK_KEYWORDS: dict[str, RiskLevel] = {
        "mandatory":   RiskLevel.HIGH,
        "must":        RiskLevel.HIGH,
        "shall":       RiskLevel.HIGH,
        "penalty":     RiskLevel.HIGH,
        "fine":        RiskLevel.HIGH,
        "revoke":      RiskLevel.HIGH,
        "should":      RiskLevel.MEDIUM,
        "recommended": RiskLevel.MEDIUM,
        "may":         RiskLevel.LOW,
        "optional":    RiskLevel.LOW,
    }

    DEADLINE_MAP: dict[RiskLevel, int] = {
        RiskLevel.HIGH:   14,
        RiskLevel.MEDIUM: 30,
        RiskLevel.LOW:    60,
    }

    def apply(self, action: ActionItem, obligation: Obligation) -> ActionItem:
        """Apply SME rules to enrich an action item."""
        text_lower = (obligation.description + " " + action.description).lower()

        # Assign department
        for keyword, dept in self.DEPARTMENT_MAP.items():
            if keyword in text_lower:
                action.owner_dept = dept
                break
        if not action.owner_dept:
            action.owner_dept = "Compliance"

        # Determine risk level
        for keyword, risk in self.RISK_KEYWORDS.items():
            if keyword in text_lower:
                action.risk_level = risk
                break

        # Set deadline based on risk
        action.deadline_days = self.DEADLINE_MAP.get(
            action.risk_level, 30
        )

        # Determine evidence type
        if any(k in text_lower for k in ["report", "audit", "review"]):
            action.evidence_type = "audit_report"
        elif any(k in text_lower for k in ["policy", "document", "procedure"]):
            action.evidence_type = "policy_document"
        elif any(k in text_lower for k in ["system", "config", "technical"]):
            action.evidence_type = "system_config"
        else:
            action.evidence_type = "compliance_certificate"

        return action


# ──────────────────────────────────────────────────────────────────────────────
# Stage 1: Obligation Extraction
# ──────────────────────────────────────────────────────────────────────────────

class ObligationExtractor:
    """Extract regulatory obligations from parsed text using LLM."""

    OBLIGATION_MARKERS = re.compile(
        r"\b(?:shall|must|required to|obligated to|mandatory|"
        r"is required|are required|needs to|directed to)\b",
        re.IGNORECASE,
    )

    def __init__(self, llm_client: Any = None):
        self._llm = llm_client

    async def extract(self, text: str, doc_id: str) -> list[Obligation]:
        """Extract obligations using LLM with regex pre-filtering."""
        if self._llm:
            return await self._extract_with_llm(text, doc_id)
        return self._extract_rule_based(text, doc_id)

    async def _extract_with_llm(self, text: str, doc_id: str) -> list[Obligation]:
        """Use LLM for obligation extraction."""
        prompt = (
            "You are a regulatory compliance expert. Extract all regulatory "
            "obligations from the following text. For each obligation provide:\n"
            "- regulation_ref: the regulation or circular reference\n"
            "- description: clear description of what must be done\n"
            "- keywords: relevant compliance keywords\n"
            "- confidence: your confidence score (0.0-1.0)\n\n"
            "Return as JSON array.\n\n"
            f"Text:\n{text[:8000]}"
        )
        try:
            response = await self._llm.ainvoke(prompt)
            content = response.content if hasattr(response, 'content') else str(response)
            # Parse JSON from response
            json_match = re.search(r'\[.*\]', content, re.DOTALL)
            if json_match:
                items = json.loads(json_match.group())
                return [
                    Obligation(
                        obligation_id=str(uuid.uuid4()),
                        source_text=text[:200],
                        regulation_ref=item.get("regulation_ref", ""),
                        description=item.get("description", ""),
                        confidence=float(item.get("confidence", 0.7)),
                        keywords=item.get("keywords", []),
                    )
                    for item in items
                ]
        except Exception as exc:
            logger.error("LLM obligation extraction failed: %s", exc)

        return self._extract_rule_based(text, doc_id)

    def _extract_rule_based(self, text: str, doc_id: str) -> list[Obligation]:
        """Rule-based fallback for obligation extraction."""
        obligations: list[Obligation] = []
        sentences = re.split(r'(?<=[.!?])\s+', text)

        for sentence in sentences:
            if self.OBLIGATION_MARKERS.search(sentence):
                obligations.append(Obligation(
                    obligation_id=str(uuid.uuid4()),
                    source_text=sentence.strip(),
                    regulation_ref=self._extract_ref(sentence),
                    description=sentence.strip(),
                    confidence=0.75,
                    keywords=self._extract_keywords(sentence),
                ))

        logger.info("Extracted %d obligations from doc %s", len(obligations), doc_id)
        return obligations

    @staticmethod
    def _extract_ref(text: str) -> str:
        match = re.search(
            r'(?:RBI|SEBI|CERT-In|DPDP|IRDAI)[\s/\-]*(?:Circular|Notification|'
            r'Master Direction|Guideline)[\s/\-]*(?:No\.?\s*)?[\w\-/]+',
            text, re.IGNORECASE
        )
        return match.group() if match else "unspecified"

    @staticmethod
    def _extract_keywords(text: str) -> list[str]:
        kw_pattern = re.compile(
            r'\b(?:KYC|AML|CFT|encryption|audit|compliance|data protection|'
            r'cyber|NPA|capital|liquidity|risk|fraud|outsourcing)\b',
            re.IGNORECASE,
        )
        return list(set(m.group().lower() for m in kw_pattern.finditer(text)))


# ──────────────────────────────────────────────────────────────────────────────
# Stage 2: Decomposition
# ──────────────────────────────────────────────────────────────────────────────

class ObligationDecomposer:
    """Decompose obligations into atomic action items."""

    def __init__(self, llm_client: Any = None):
        self._llm = llm_client

    async def decompose(self, obligations: list[Obligation]) -> list[ActionItem]:
        """Decompose each obligation into action items."""
        all_actions: list[ActionItem] = []

        for ob in obligations:
            if self._llm:
                actions = await self._decompose_with_llm(ob)
            else:
                actions = self._decompose_rule_based(ob)
            all_actions.extend(actions)

        logger.info("Decomposed %d obligations into %d actions",
                     len(obligations), len(all_actions))
        return all_actions

    async def _decompose_with_llm(self, obligation: Obligation) -> list[ActionItem]:
        """Use LLM to decompose an obligation."""
        prompt = (
            "Decompose this regulatory obligation into specific, measurable "
            "action items. For each action provide:\n"
            "- description: what exactly needs to be done\n"
            "- evidence_type: what evidence proves completion\n\n"
            f"Obligation: {obligation.description}\n"
            "Return as JSON array."
        )
        try:
            response = await self._llm.ainvoke(prompt)
            content = response.content if hasattr(response, 'content') else str(response)
            json_match = re.search(r'\[.*\]', content, re.DOTALL)
            if json_match:
                items = json.loads(json_match.group())
                return [
                    ActionItem(
                        action_id=str(uuid.uuid4()),
                        obligation_id=obligation.obligation_id,
                        description=item.get("description", ""),
                        evidence_type=item.get("evidence_type", ""),
                    )
                    for item in items
                ]
        except Exception as exc:
            logger.error("LLM decomposition failed: %s", exc)

        return self._decompose_rule_based(obligation)

    def _decompose_rule_based(self, obligation: Obligation) -> list[ActionItem]:
        """Rule-based decomposition fallback."""
        return [ActionItem(
            action_id=str(uuid.uuid4()),
            obligation_id=obligation.obligation_id,
            description=obligation.description,
        )]


# ──────────────────────────────────────────────────────────────────────────────
# Stage 5: Guardrails Validation
# ──────────────────────────────────────────────────────────────────────────────

class GuardrailsValidator:
    """
    Validates MAP output quality and triggers escalation if confidence
    falls below CONFIDENCE_THRESHOLD (0.80).
    """

    MAP_SCHEMA = {
        "required_fields": ["map_id", "doc_id", "obligations", "action_items"],
        "min_obligations": 1,
        "min_actions": 1,
    }

    def validate(self, map_doc: MAPDocument) -> MAPDocument:
        """Validate and potentially escalate the MAP document."""
        errors: list[str] = []

        # Schema checks
        if not map_doc.obligations:
            errors.append("No obligations extracted")
        if not map_doc.action_items:
            errors.append("No action items generated")

        # Check action items have required fields
        for action in map_doc.action_items:
            if not action.owner_dept:
                errors.append(f"Action {action.action_id}: missing owner_dept")
            if not action.evidence_type:
                errors.append(f"Action {action.action_id}: missing evidence_type")

        # Compute overall confidence
        if map_doc.obligations:
            avg_conf = sum(o.confidence for o in map_doc.obligations) / len(map_doc.obligations)
            map_doc.confidence = avg_conf
        else:
            map_doc.confidence = 0.0

        map_doc.errors = errors

        # Escalation check — CONFIDENCE_THRESHOLD = 0.80
        if map_doc.confidence < CONFIDENCE_THRESHOLD:
            map_doc.escalated = True
            map_doc.escalation_reason = (
                f"Overall confidence {map_doc.confidence:.2%} is below "
                f"threshold {CONFIDENCE_THRESHOLD:.0%}. "
                "Escalating to Compliance Officer for manual review."
            )
            map_doc.status = MAPStatus.ESCALATED
            logger.warning("MAP %s ESCALATED: %s", map_doc.map_id,
                           map_doc.escalation_reason)
        elif errors:
            map_doc.status = MAPStatus.FAILED
            logger.error("MAP %s FAILED validation: %s", map_doc.map_id, errors)
        else:
            map_doc.status = MAPStatus.VALIDATED
            logger.info("MAP %s VALIDATED (confidence=%.2f)",
                         map_doc.map_id, map_doc.confidence)

        return map_doc


# ──────────────────────────────────────────────────────────────────────────────
# MAP Generator — 5-Stage Pipeline Orchestrator
# ──────────────────────────────────────────────────────────────────────────────

class MAPGenerator:
    """
    Orchestrates the 5-stage MAP generation pipeline:
      1. Obligation Extraction
      2. Decomposition
      3. SME Rules Application
      4. JSON Structuring
      5. Guardrails Validation

    Usage::

        generator = MAPGenerator(llm_client=my_llm)
        result = await generator.generate(doc_id="doc-001", text="...")
    """

    def __init__(self, llm_client: Any = None):
        self._extractor = ObligationExtractor(llm_client)
        self._decomposer = ObligationDecomposer(llm_client)
        self._sme_engine = SMERulesEngine()
        self._guardrails = GuardrailsValidator()

    async def generate(self, doc_id: str, text: str) -> MAPDocument:
        """Run the full 5-stage pipeline."""
        map_doc = MAPDocument(
            map_id=str(uuid.uuid4()),
            doc_id=doc_id,
        )

        try:
            # Stage 1: Obligation Extraction
            logger.info("Stage 1: Extracting obligations from %s", doc_id)
            obligations = await self._extractor.extract(text, doc_id)
            map_doc.obligations = obligations

            # Stage 2: Decomposition
            logger.info("Stage 2: Decomposing %d obligations", len(obligations))
            actions = await self._decomposer.decompose(obligations)

            # Stage 3: SME Rules Application
            logger.info("Stage 3: Applying SME rules to %d actions", len(actions))
            for action in actions:
                ob = next(
                    (o for o in obligations if o.obligation_id == action.obligation_id),
                    obligations[0] if obligations else Obligation(
                        obligation_id="", source_text="", regulation_ref="",
                        description=""
                    ),
                )
                action = self._sme_engine.apply(action, ob)
            map_doc.action_items = actions

            # Stage 4: JSON Structuring (implicit — the dataclass handles this)
            logger.info("Stage 4: Structuring MAP JSON output")

            # Stage 5: Guardrails Validation
            logger.info("Stage 5: Running guardrails validation")
            map_doc = self._guardrails.validate(map_doc)

        except Exception as exc:
            map_doc.status = MAPStatus.FAILED
            map_doc.errors.append(f"Pipeline error: {exc}")
            logger.error("MAP generation failed: %s", exc)

        return map_doc
