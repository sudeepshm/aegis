"""
routing/router.py
=================
Hybrid rule-based + ML department assignment and hierarchy resolution.

Architecture
────────────
  ┌──────────────────────────────────────┐
  │         HybridRouter                 │
  │  ┌────────────────────────────────┐  │
  │  │  Rule Engine (deterministic)   │  │
  │  │  Regulation→Department mapping │  │
  │  └────────────────────────────────┘  │
  │  ┌────────────────────────────────┐  │
  │  │  ML Classifier (fallback)     │  │
  │  │  TF-IDF + keyword scoring     │  │
  │  └────────────────────────────────┘  │
  │  ┌────────────────────────────────┐  │
  │  │  Hierarchy Resolver            │  │
  │  │  Escalation chain management   │  │
  │  └────────────────────────────────┘  │
  └──────────────────────────────────────┘

Dependencies
────────────
  pip install scikit-learn (optional, for ML classifier)
"""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

logger = logging.getLogger(__name__)

CONFIDENCE_THRESHOLD = 0.80  # below this → escalate to Compliance Officer


# ──────────────────────────────────────────────────────────────────────────────
# Data Structures
# ──────────────────────────────────────────────────────────────────────────────

class Priority(str, Enum):
    CRITICAL = "1-Critical"
    HIGH     = "2-High"
    MEDIUM   = "3-Medium"
    LOW      = "4-Low"


@dataclass
class DepartmentInfo:
    """Department metadata for routing."""
    dept_id:        str
    name:           str
    head_email:     str
    head_name:      str
    escalation_to:  str = ""          # dept_id of escalation target
    sla_hours:      dict[str, int] = field(default_factory=dict)


@dataclass
class RoutedTask:
    """Output of the routing engine."""
    task_id:           str
    obligation_id:     str
    department:        str
    department_head:   str
    head_email:        str
    priority:          Priority
    sla_hours:         int
    confidence:        float
    escalated:         bool = False
    escalation_reason: str = ""
    routing_method:    str = ""       # "rule" | "ml" | "escalated"
    metadata:          dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id":           self.task_id,
            "obligation_id":    self.obligation_id,
            "department":       self.department,
            "department_head":  self.department_head,
            "head_email":       self.head_email,
            "priority":         self.priority.value,
            "sla_hours":        self.sla_hours,
            "confidence":       round(self.confidence, 4),
            "escalated":        self.escalated,
            "escalation_reason": self.escalation_reason,
            "routing_method":   self.routing_method,
        }


# ──────────────────────────────────────────────────────────────────────────────
# Organisation Hierarchy
# ──────────────────────────────────────────────────────────────────────────────

class OrganisationHierarchy:
    """Manages department registry and escalation chains."""

    def __init__(self) -> None:
        self._departments: dict[str, DepartmentInfo] = {}
        self._load_defaults()

    def _load_defaults(self) -> None:
        defaults = [
            DepartmentInfo("compliance", "Compliance", "cco@bank.org",
                           "Chief Compliance Officer", "board",
                           {"1-Critical": 4, "2-High": 8, "3-Medium": 24, "4-Low": 72}),
            DepartmentInfo("it-security", "IT Security", "ciso@bank.org",
                           "CISO", "compliance",
                           {"1-Critical": 2, "2-High": 4, "3-Medium": 12, "4-Low": 48}),
            DepartmentInfo("finance", "Finance", "cfo@bank.org",
                           "CFO", "compliance",
                           {"1-Critical": 4, "2-High": 8, "3-Medium": 24, "4-Low": 72}),
            DepartmentInfo("operations", "Operations", "coo@bank.org",
                           "COO", "compliance",
                           {"1-Critical": 4, "2-High": 12, "3-Medium": 24, "4-Low": 72}),
            DepartmentInfo("credit-risk", "Credit Risk", "cro@bank.org",
                           "Chief Risk Officer", "compliance",
                           {"1-Critical": 4, "2-High": 8, "3-Medium": 24, "4-Low": 48}),
            DepartmentInfo("treasury", "Treasury", "treasury-head@bank.org",
                           "Treasury Head", "finance",
                           {"1-Critical": 2, "2-High": 6, "3-Medium": 18, "4-Low": 48}),
            DepartmentInfo("internal-audit", "Internal Audit", "cia@bank.org",
                           "Chief Internal Auditor", "board",
                           {"1-Critical": 4, "2-High": 8, "3-Medium": 24, "4-Low": 72}),
            DepartmentInfo("vendor-mgmt", "Vendor Management", "vendor-head@bank.org",
                           "Vendor Management Head", "operations",
                           {"1-Critical": 8, "2-High": 16, "3-Medium": 48, "4-Low": 96}),
            DepartmentInfo("customer-svc", "Customer Service", "cs-head@bank.org",
                           "CS Head", "operations",
                           {"1-Critical": 4, "2-High": 8, "3-Medium": 24, "4-Low": 72}),
            DepartmentInfo("fraud-mgmt", "Fraud Management", "fraud-head@bank.org",
                           "Fraud Head", "compliance",
                           {"1-Critical": 2, "2-High": 4, "3-Medium": 12, "4-Low": 48}),
            DepartmentInfo("board", "Board / MD", "md@bank.org",
                           "Managing Director", "",
                           {"1-Critical": 4, "2-High": 8, "3-Medium": 24, "4-Low": 72}),
        ]
        for dept in defaults:
            self._departments[dept.dept_id] = dept

    def get(self, dept_id: str) -> Optional[DepartmentInfo]:
        return self._departments.get(dept_id)

    def get_escalation_chain(self, dept_id: str) -> list[DepartmentInfo]:
        """Return the full escalation chain from dept up to board."""
        chain: list[DepartmentInfo] = []
        visited: set[str] = set()
        current = dept_id
        while current and current not in visited:
            visited.add(current)
            dept = self._departments.get(current)
            if dept:
                chain.append(dept)
                current = dept.escalation_to
            else:
                break
        return chain

    def resolve_sla(self, dept_id: str, priority: str) -> int:
        dept = self._departments.get(dept_id)
        if dept and priority in dept.sla_hours:
            return dept.sla_hours[priority]
        return 24  # default


# ──────────────────────────────────────────────────────────────────────────────
# Rule Engine
# ──────────────────────────────────────────────────────────────────────────────

class RuleEngine:
    """Deterministic keyword-to-department mapping."""

    RULES: list[tuple[list[str], str, float]] = [
        # (keywords, dept_id, base_confidence)
        (["kyc", "know your customer", "customer due diligence", "cdd"],
         "operations", 0.95),
        (["aml", "anti-money laundering", "pmla", "suspicious transaction"],
         "compliance", 0.95),
        (["encryption", "tls", "cyber", "data breach", "vulnerability",
          "cert-in", "information security", "iso 27001"],
         "it-security", 0.95),
        (["capital adequacy", "crar", "basel", "pillar", "tier 1", "tier 2"],
         "finance", 0.92),
        (["npa", "non-performing", "provisioning", "irac", "asset classification"],
         "credit-risk", 0.92),
        (["liquidity", "lcr", "nsfr", "alm", "interest rate risk"],
         "treasury", 0.90),
        (["audit", "inspection", "statutory audit", "concurrent audit"],
         "internal-audit", 0.90),
        (["outsourcing", "vendor", "third party", "service provider"],
         "vendor-mgmt", 0.88),
        (["grievance", "complaint", "customer protection", "ombudsman"],
         "customer-svc", 0.88),
        (["fraud", "forgery", "counterfeit", "phishing"],
         "fraud-mgmt", 0.92),
        (["data protection", "dpdp", "gdpr", "personal data", "consent"],
         "it-security", 0.90),
    ]

    def match(self, text: str) -> tuple[Optional[str], float]:
        """
        Match text against rules.
        Returns (dept_id, confidence) or (None, 0.0).
        """
        text_lower = text.lower()
        best_dept: Optional[str] = None
        best_conf = 0.0
        best_hits = 0

        for keywords, dept_id, base_conf in self.RULES:
            hits = sum(1 for kw in keywords if kw in text_lower)
            if hits > 0:
                # Boost confidence with more keyword matches
                conf = min(base_conf + (hits - 1) * 0.02, 0.99)
                if hits > best_hits or (hits == best_hits and conf > best_conf):
                    best_dept = dept_id
                    best_conf = conf
                    best_hits = hits

        return best_dept, best_conf


# ──────────────────────────────────────────────────────────────────────────────
# ML Classifier (lightweight keyword-scoring fallback)
# ──────────────────────────────────────────────────────────────────────────────

class MLClassifier:
    """
    Lightweight keyword-frequency-based classifier.
    Acts as ML fallback when rule engine has low confidence.
    """

    # Department keyword profiles (TF-IDF inspired)
    PROFILES: dict[str, list[str]] = {
        "compliance":     ["regulation", "compliance", "regulatory", "rbi", "sebi",
                           "circular", "guideline", "notification", "directive"],
        "it-security":    ["security", "cyber", "encryption", "firewall", "access",
                           "authentication", "mfa", "vulnerability", "patch"],
        "finance":        ["capital", "financial", "reserve", "provision", "ratio",
                           "adequacy", "buffer", "tier", "equity"],
        "credit-risk":    ["credit", "loan", "npa", "default", "recovery",
                           "restructuring", "write-off", "classification"],
        "operations":     ["process", "customer", "account", "transaction",
                           "verification", "onboarding", "kyc"],
        "treasury":       ["liquidity", "market", "interest", "rate", "maturity",
                           "investment", "portfolio", "bond"],
        "fraud-mgmt":     ["fraud", "suspicious", "alert", "monitoring",
                           "detection", "investigation"],
    }

    def classify(self, text: str) -> tuple[str, float]:
        """Classify text into a department with confidence score."""
        text_lower = text.lower()
        words = set(re.findall(r'\b\w+\b', text_lower))

        scores: dict[str, float] = {}
        for dept, profile in self.PROFILES.items():
            hits = sum(1 for kw in profile if kw in words)
            scores[dept] = hits / len(profile) if profile else 0.0

        if not scores or max(scores.values()) == 0:
            return "compliance", 0.50

        best_dept = max(scores, key=scores.get)
        raw_score = scores[best_dept]
        confidence = min(0.50 + raw_score * 0.45, 0.95)

        return best_dept, confidence


# ──────────────────────────────────────────────────────────────────────────────
# Hybrid Router
# ──────────────────────────────────────────────────────────────────────────────

class HybridRouter:
    """
    Routes compliance tasks to departments using:
      1. Rule engine (high confidence, deterministic)
      2. ML classifier (fallback when rules don't match)
      3. Escalation to Compliance Officer if confidence < 0.80

    Usage::

        router = HybridRouter()
        task = router.route(
            obligation_id="ob-001",
            text="RBI circular on cyber security framework",
            risk_level="HIGH",
        )
    """

    def __init__(self) -> None:
        self._rule_engine = RuleEngine()
        self._ml_classifier = MLClassifier()
        self._hierarchy = OrganisationHierarchy()

    def route(self, obligation_id: str, text: str,
              risk_level: str = "MEDIUM") -> RoutedTask:
        """Route an obligation to the appropriate department."""
        task_id = f"task-{uuid.uuid4().hex[:12]}"

        # Determine priority from risk level
        priority = self._resolve_priority(risk_level)

        # Step 1: Try rule engine
        dept_id, confidence = self._rule_engine.match(text)
        method = "rule"

        # Step 2: Fall back to ML if rule confidence is low
        if dept_id is None or confidence < CONFIDENCE_THRESHOLD:
            ml_dept, ml_conf = self._ml_classifier.classify(text)
            if dept_id is None or ml_conf > confidence:
                dept_id = ml_dept
                confidence = ml_conf
                method = "ml"

        # Step 3: Resolve department info
        dept_info = self._hierarchy.get(dept_id or "compliance")
        if not dept_info:
            dept_info = self._hierarchy.get("compliance")

        # Step 4: Resolve SLA
        sla = self._hierarchy.resolve_sla(dept_info.dept_id, priority.value)

        # Step 5: Check for escalation (confidence < 0.80)
        escalated = confidence < CONFIDENCE_THRESHOLD
        escalation_reason = ""
        if escalated:
            escalation_reason = (
                f"Routing confidence {confidence:.2%} is below threshold "
                f"{CONFIDENCE_THRESHOLD:.0%}. Escalating to Compliance Officer."
            )
            # Override to compliance department
            dept_info = self._hierarchy.get("compliance")
            method = "escalated"
            logger.warning("Task %s ESCALATED: %s", task_id, escalation_reason)

        task = RoutedTask(
            task_id=task_id,
            obligation_id=obligation_id,
            department=dept_info.name,
            department_head=dept_info.head_name,
            head_email=dept_info.head_email,
            priority=priority,
            sla_hours=sla,
            confidence=confidence,
            escalated=escalated,
            escalation_reason=escalation_reason,
            routing_method=method,
        )

        logger.info(
            "Routed: task=%s dept=%s priority=%s sla=%dh conf=%.2f method=%s",
            task_id, dept_info.name, priority.value, sla, confidence, method,
        )
        return task

    def route_batch(self, items: list[dict[str, Any]]) -> list[RoutedTask]:
        """Route multiple obligations."""
        return [
            self.route(
                obligation_id=item["obligation_id"],
                text=item["text"],
                risk_level=item.get("risk_level", "MEDIUM"),
            )
            for item in items
        ]

    def get_escalation_chain(self, dept_name: str) -> list[dict[str, str]]:
        """Get the escalation chain for a department."""
        # Find dept_id by name
        for dept in self._hierarchy._departments.values():
            if dept.name.lower() == dept_name.lower():
                chain = self._hierarchy.get_escalation_chain(dept.dept_id)
                return [{"name": d.name, "head": d.head_name,
                         "email": d.head_email} for d in chain]
        return []

    @staticmethod
    def _resolve_priority(risk_level: str) -> Priority:
        mapping = {
            "HIGH":   Priority.CRITICAL,
            "MEDIUM": Priority.HIGH,
            "LOW":    Priority.MEDIUM,
        }
        return mapping.get(risk_level.upper(), Priority.MEDIUM)
