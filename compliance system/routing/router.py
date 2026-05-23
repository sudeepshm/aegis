"""
routing/router.py

Hybrid Routing Engine — combines a deterministic rule-based classifier with
a contextual ML fallback to route regulatory/compliance directives to the
correct organisational units, resolve hierarchy, and build RACI-enriched
task graphs with cross-functional dependency linking.

Architecture
────────────
  ┌─────────────────────────────────────┐
  │           HybridRouter              │
  │                                     │
  │  ┌──────────────┐  conf >= 0.85     │
  │  │  RuleEngine  │──────────────────►│
  │  └──────────────┘                   │
  │         │ conf < 0.85               │
  │         ▼                           │
  │  ┌──────────────────┐               │
  │  │  MLClassifier    │               │
  │  └──────────────────┘               │
  │         │                           │
  │         ▼                           │
  │  ┌──────────────────┐               │
  │  │ HierarchyResolver│               │
  │  └──────────────────┘               │
  │         │                           │
  │         ▼                           │
  │  ┌────────────────────────┐         │
  │  │ CrossFunctionalResolver│         │
  │  └────────────────────────┘         │
  │         │                           │
  │         ▼                           │
  │  ┌─────────────────┐                │
  │  │ ExceptionHandler│                │
  │  └─────────────────┘                │
  └─────────────────────────────────────┘
"""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline

# ─────────────────────────────────────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s – %(message)s",
)
logger = logging.getLogger("router")

# ─────────────────────────────────────────────────────────────────────────────
# Thresholds
# ─────────────────────────────────────────────────────────────────────────────
RULE_CONFIDENCE_THRESHOLD = 0.85   # Below → fall back to ML
ROUTE_CONFIDENCE_THRESHOLD = 0.80  # Below → UNASSIGNED / Compliance Officer
CROSS_FUNCTIONAL_TOP_K = 2         # Max departments for cross-functional split


# ─────────────────────────────────────────────────────────────────────────────
# Enumerations
# ─────────────────────────────────────────────────────────────────────────────

class Department(str, Enum):
    RETAIL_BANKING = "Retail Banking"
    IT             = "IT"
    TREASURY       = "Treasury"
    CORPORATE      = "Corporate"
    COMPLIANCE     = "Compliance"      # fallback / UNASSIGNED owner
    UNASSIGNED     = "UNASSIGNED"


class RACIRole(str, Enum):
    RESPONSIBLE = "Responsible"
    ACCOUNTABLE = "Accountable"
    CONSULTED   = "Consulted"
    INFORMED    = "Informed"


class TaskStatus(str, Enum):
    PENDING    = "PENDING"
    BLOCKED    = "BLOCKED"     # has unresolved dependencies
    UNASSIGNED = "UNASSIGNED"  # low-confidence route


# ─────────────────────────────────────────────────────────────────────────────
# Hierarchy Catalogue
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class UnitHead:
    title: str
    name: str
    email: str


@dataclass(frozen=True)
class SubUnit:
    name: str
    lead: str
    email: str


@dataclass(frozen=True)
class DepartmentProfile:
    department: Department
    unit_head: UnitHead
    sub_units: list[SubUnit]
    default_consulted: list[str]      # titles / teams always consulted
    default_informed:  list[str]      # titles / teams always informed


HIERARCHY: dict[Department, DepartmentProfile] = {
    Department.RETAIL_BANKING: DepartmentProfile(
        department=Department.RETAIL_BANKING,
        unit_head=UnitHead(
            title="Chief of Retail Banking",
            name="Alexandra Patel",
            email="chief.retail@bank.org",
        ),
        sub_units=[
            SubUnit("KYC & Onboarding",      "Head of KYC",          "kyc@bank.org"),
            SubUnit("Branch Operations",      "Head of Branches",     "branches@bank.org"),
            SubUnit("Retail Credit",          "Head of Retail Credit","retail.credit@bank.org"),
            SubUnit("Customer Experience",    "Head of CX",           "cx@bank.org"),
        ],
        default_consulted=["Legal", "Risk Management"],
        default_informed=["Internal Audit", "Board Risk Committee"],
    ),
    Department.IT: DepartmentProfile(
        department=Department.IT,
        unit_head=UnitHead(
            title="CISO",
            name="Marcus Okonkwo",
            email="ciso@bank.org",
        ),
        sub_units=[
            SubUnit("Cybersecurity",          "Head of Cybersecurity",  "cybersec@bank.org"),
            SubUnit("Data Engineering",       "Head of Data Eng",       "data.eng@bank.org"),
            SubUnit("Infrastructure & Cloud", "Head of Infra",          "infra@bank.org"),
            SubUnit("IT Risk & Compliance",   "Head of IT Risk",        "it.risk@bank.org"),
        ],
        default_consulted=["Legal", "Compliance"],
        default_informed=["Internal Audit", "CTO Office"],
    ),
    Department.TREASURY: DepartmentProfile(
        department=Department.TREASURY,
        unit_head=UnitHead(
            title="Chief Treasury Officer",
            name="Priya Sundaram",
            email="cto.treasury@bank.org",
        ),
        sub_units=[
            SubUnit("Liquidity Management",   "Head of Liquidity",      "liquidity@bank.org"),
            SubUnit("Capital Markets",        "Head of Capital Markets","cap.markets@bank.org"),
            SubUnit("ALM",                    "Head of ALM",            "alm@bank.org"),
            SubUnit("FX & Derivatives",       "Head of FX",             "fx@bank.org"),
        ],
        default_consulted=["Risk Management", "Compliance"],
        default_informed=["CFO Office", "Internal Audit"],
    ),
    Department.CORPORATE: DepartmentProfile(
        department=Department.CORPORATE,
        unit_head=UnitHead(
            title="Chief Corporate Banking Officer",
            name="Jonathan Ferreira",
            email="chief.corporate@bank.org",
        ),
        sub_units=[
            SubUnit("Corporate Credit",       "Head of Corp Credit",    "corp.credit@bank.org"),
            SubUnit("Trade Finance",          "Head of Trade Finance",  "trade.finance@bank.org"),
            SubUnit("Syndications",           "Head of Syndications",   "syndications@bank.org"),
            SubUnit("Relationship Management","Head of RM",             "rm@bank.org"),
        ],
        default_consulted=["Legal", "Risk Management"],
        default_informed=["CEO Office", "Internal Audit"],
    ),
    Department.COMPLIANCE: DepartmentProfile(
        department=Department.COMPLIANCE,
        unit_head=UnitHead(
            title="Chief Compliance Officer",
            name="Simone Leclerc",
            email="cco@bank.org",
        ),
        sub_units=[
            SubUnit("AML / CFT",              "Head of AML",            "aml@bank.org"),
            SubUnit("Regulatory Reporting",   "Head of Reg Reporting",  "reg.reporting@bank.org"),
            SubUnit("Policy & Governance",    "Head of Policy",         "policy@bank.org"),
        ],
        default_consulted=["Legal", "Internal Audit"],
        default_informed=["Board Risk Committee", "Regulators"],
    ),
}


# ─────────────────────────────────────────────────────────────────────────────
# Rule Engine
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class RuleMatch:
    department: Department
    matched_keywords: list[str]
    confidence: float
    sub_unit_hint: str | None = None  # first sub-unit keyword maps to


# Keyword → (Department, optional sub-unit name, base confidence weight)
KEYWORD_RULES: list[tuple[re.Pattern, Department, str | None, float]] = [
    # Retail Banking
    (re.compile(r"\bKYC\b",                   re.I), Department.RETAIL_BANKING, "KYC & Onboarding",    0.95),
    (re.compile(r"\bonboarding\b",            re.I), Department.RETAIL_BANKING, "KYC & Onboarding",    0.90),
    (re.compile(r"\bretail\b",                re.I), Department.RETAIL_BANKING, None,                  0.88),
    (re.compile(r"\bcustomer\s+due\s+diligence\b|CDD\b", re.I),
                                                      Department.RETAIL_BANKING, "KYC & Onboarding",    0.93),
    (re.compile(r"\bbranch\b",                re.I), Department.RETAIL_BANKING, "Branch Operations",   0.87),

    # IT / Cybersecurity
    (re.compile(r"\bencryption\b",            re.I), Department.IT,             "Cybersecurity",        0.96),
    (re.compile(r"\bcybersecurity\b",         re.I), Department.IT,             "Cybersecurity",        0.97),
    (re.compile(r"\bdata\s+breach\b",         re.I), Department.IT,             "Cybersecurity",        0.95),
    (re.compile(r"\binfrastructure\b",        re.I), Department.IT,             "Infrastructure & Cloud",0.89),
    (re.compile(r"\bcloud\b",                 re.I), Department.IT,             "Infrastructure & Cloud",0.88),
    (re.compile(r"\bpatch\b|\bvulnerabilit",  re.I), Department.IT,             "Cybersecurity",        0.91),
    (re.compile(r"\baccess\s+control\b|\bIAM\b",re.I),Department.IT,            "IT Risk & Compliance", 0.92),
    (re.compile(r"\bdata\s+engineer",         re.I), Department.IT,             "Data Engineering",     0.90),

    # Treasury
    (re.compile(r"\bliquidity\b",             re.I), Department.TREASURY,       "Liquidity Management", 0.94),
    (re.compile(r"\bcapital\s+market",        re.I), Department.TREASURY,       "Capital Markets",      0.93),
    (re.compile(r"\bALM\b|\basset.liability", re.I), Department.TREASURY,       "ALM",                  0.95),
    (re.compile(r"\bFX\b|\bforeign\s+exchange",re.I),Department.TREASURY,       "FX & Derivatives",     0.93),
    (re.compile(r"\bderivative",              re.I), Department.TREASURY,       "FX & Derivatives",     0.91),
    (re.compile(r"\btreasury\b",              re.I), Department.TREASURY,       None,                   0.88),

    # Corporate
    (re.compile(r"\bcorporate\b",             re.I), Department.CORPORATE,      None,                   0.86),
    (re.compile(r"\btrade\s+finance\b",       re.I), Department.CORPORATE,      "Trade Finance",        0.94),
    (re.compile(r"\bsyndication",             re.I), Department.CORPORATE,      "Syndications",         0.92),
    (re.compile(r"\bcredit\s+facilit",        re.I), Department.CORPORATE,      "Corporate Credit",     0.89),

    # Compliance (always adds a consult signal)
    (re.compile(r"\bAML\b|\banti.money\s+launder",re.I),Department.COMPLIANCE,  "AML / CFT",           0.96),
    (re.compile(r"\bregulat",                 re.I), Department.COMPLIANCE,     "Regulatory Reporting", 0.87),
    (re.compile(r"\bcomplian",                re.I), Department.COMPLIANCE,     "Policy & Governance",  0.88),
]


class RuleEngine:
    """
    Deterministic keyword matcher.
    Returns per-department confidence by aggregating weighted keyword hits.
    """

    def classify(self, text: str) -> list[RuleMatch]:
        dept_scores: dict[Department, dict] = {}

        for pattern, dept, sub_unit, weight in KEYWORD_RULES:
            matches = pattern.findall(text)
            if not matches:
                continue

            if dept not in dept_scores:
                dept_scores[dept] = {
                    "score":    0.0,
                    "keywords": [],
                    "sub_unit": None,
                    "hits":     0,
                }

            entry = dept_scores[dept]
            # Diminishing returns on repeated hits of same keyword group
            increment = weight * (0.9 ** entry["hits"])
            entry["score"]    = min(1.0, entry["score"] + increment * 0.15)
            entry["keywords"].extend(matches)
            entry["hits"]    += 1
            if sub_unit and entry["sub_unit"] is None:
                entry["sub_unit"] = sub_unit

        # Normalise: anchor the top score to at most 0.99
        if not dept_scores:
            return []

        max_raw = max(v["score"] for v in dept_scores.values())
        results: list[RuleMatch] = []
        for dept, data in dept_scores.items():
            normalised = round((data["score"] / max_raw) * 0.99, 4) if max_raw > 0 else 0.0
            results.append(RuleMatch(
                department=dept,
                matched_keywords=list(set(data["keywords"])),
                confidence=normalised,
                sub_unit_hint=data["sub_unit"],
            ))

        results.sort(key=lambda r: r.confidence, reverse=True)
        return results


# ─────────────────────────────────────────────────────────────────────────────
# ML Classifier (TF-IDF + Logistic Regression)
# ─────────────────────────────────────────────────────────────────────────────

# Lightweight synthetic training corpus – replace with production data
_TRAINING_DATA: list[tuple[str, str]] = [
    # Retail Banking
    ("customer identity verification and KYC onboarding process", "Retail Banking"),
    ("retail branch operations and customer service standards",    "Retail Banking"),
    ("CDD requirements for new retail account opening",           "Retail Banking"),
    ("consumer lending and retail credit assessment policy",       "Retail Banking"),
    ("branch network expansion and customer experience programme", "Retail Banking"),

    # IT
    ("AES-256 encryption implementation for data at rest",        "IT"),
    ("cybersecurity incident response and vulnerability patching", "IT"),
    ("cloud infrastructure migration and access control policy",   "IT"),
    ("IAM review and privileged access management standards",      "IT"),
    ("data engineering pipeline security and governance",          "IT"),

    # Treasury
    ("liquidity coverage ratio and LCR reporting requirements",   "Treasury"),
    ("FX hedging strategy and derivative instrument management",   "Treasury"),
    ("asset-liability management and interest rate risk",          "Treasury"),
    ("capital markets issuance and bond programme management",     "Treasury"),
    ("intraday liquidity monitoring and stress testing",           "Treasury"),

    # Corporate
    ("corporate credit facility review and syndicated loan terms", "Corporate"),
    ("trade finance instrument and letter of credit processing",   "Corporate"),
    ("corporate relationship management and deal structuring",     "Corporate"),
    ("syndication process for large corporate exposures",          "Corporate"),
    ("corporate banking fee schedule and pricing governance",      "Corporate"),

    # Compliance
    ("AML transaction monitoring and suspicious activity reports", "Compliance"),
    ("regulatory reporting obligations under Basel III",           "Compliance"),
    ("compliance policy update and governance framework review",   "Compliance"),
    ("anti-money-laundering CFT controls and screening",           "Compliance"),
    ("regulatory examination preparedness and audit trail",        "Compliance"),
]


class MLClassifier:
    """
    TF-IDF + Logistic Regression classifier.
    Provides per-class probability estimates used as confidence scores.
    """

    def __init__(self) -> None:
        self._pipeline = Pipeline([
            ("tfidf", TfidfVectorizer(
                ngram_range=(1, 3),
                sublinear_tf=True,
                max_features=5_000,
            )),
            ("clf", LogisticRegression(
                max_iter=1_000,
                class_weight="balanced",
                C=1.5,
                solver="lbfgs",
            )),
        ])
        self._classes: list[str] = []
        self._train()

    def _train(self) -> None:
        texts, labels = zip(*_TRAINING_DATA)
        self._pipeline.fit(list(texts), list(labels))
        self._classes = list(self._pipeline.classes_)
        logger.info("MLClassifier trained on %d samples, classes=%s", len(texts), self._classes)

    def predict_proba(self, text: str) -> list[tuple[Department, float]]:
        """Return (Department, probability) pairs sorted descending."""
        probs: np.ndarray = self._pipeline.predict_proba([text])[0]
        results: list[tuple[Department, float]] = []
        for label, prob in zip(self._classes, probs):
            try:
                dept = Department(label)
            except ValueError:
                dept = Department.UNASSIGNED
            results.append((dept, round(float(prob), 4)))
        results.sort(key=lambda x: x[1], reverse=True)
        return results


# ─────────────────────────────────────────────────────────────────────────────
# Data Structures for Routing Output
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class RACIEntry:
    role: RACIRole
    party: str
    email: str | None = None

    def to_dict(self) -> dict:
        return {
            "role":  self.role.value,
            "party": self.party,
            "email": self.email,
        }


@dataclass
class RoutedTask:
    task_id:          str
    obligation_id:    str             # back-reference to obligation_map entry
    department:       Department
    unit_head:        UnitHead
    sub_unit:         SubUnit | None
    directive_text:   str
    routing_method:   str             # "RULE" | "ML" | "FALLBACK"
    rule_confidence:  float
    ml_confidence:    float
    final_confidence: float
    status:           TaskStatus
    raci:             list[RACIEntry]
    depends_on:       list[str]       # task_ids this task is blocked by
    dependency_note:  str | None      # human-readable dependency rationale
    created_at:       str
    metadata:         dict = field(default_factory=dict)

    @property
    def confidence(self) -> float:
        return self.final_confidence

    @confidence.setter
    def confidence(self, val: float) -> None:
        self.final_confidence = val

    def to_dict(self) -> dict:
        return {
            "task_id":          self.task_id,
            "obligation_id":    self.obligation_id,
            "department":       self.department.value,
            "unit_head": {
                "title": self.unit_head.title,
                "name":  self.unit_head.name,
                "email": self.unit_head.email,
            },
            "sub_unit": {
                "name":  self.sub_unit.name,
                "lead":  self.sub_unit.lead,
                "email": self.sub_unit.email,
            } if self.sub_unit else None,
            "directive_text":   self.directive_text,
            "routing_method":   self.routing_method,
            "rule_confidence":  self.rule_confidence,
            "ml_confidence":    self.ml_confidence,
            "final_confidence": self.final_confidence,
            "status":           self.status.value,
            "raci":             [r.to_dict() for r in self.raci],
            "depends_on":       self.depends_on,
            "dependency_note":  self.dependency_note,
            "created_at":       self.created_at,
            "metadata":         self.metadata,
        }


@dataclass
class DependencyEdge:
    from_task_id: str
    to_task_id:   str
    relationship: str   # e.g., "BLOCKS", "INFORMS", "PARALLEL"
    rationale:    str

    def to_dict(self) -> dict:
        return {
            "from":         self.from_task_id,
            "to":           self.to_task_id,
            "relationship": self.relationship,
            "rationale":    self.rationale,
        }


@dataclass
class RoutingResult:
    routing_id:       str
    source_text:      str
    obligation_id:    str
    tasks:            list[RoutedTask]
    dependency_graph: list[DependencyEdge]
    is_cross_functional: bool
    is_unassigned:       bool
    router_notes:        list[str]

    def to_dict(self) -> dict:
        return {
            "routing_id":           self.routing_id,
            "obligation_id":        self.obligation_id,
            "source_text":          self.source_text[:300],    # truncate for display
            "is_cross_functional":  self.is_cross_functional,
            "is_unassigned":        self.is_unassigned,
            "tasks":                [t.to_dict() for t in self.tasks],
            "dependency_graph":     [e.to_dict() for e in self.dependency_graph],
            "router_notes":         self.router_notes,
        }


# ─────────────────────────────────────────────────────────────────────────────
# Hierarchy Resolver
# ─────────────────────────────────────────────────────────────────────────────

class HierarchyResolver:
    """
    Resolves department → unit head + best-matching sub-unit.
    """

    def resolve(
        self,
        department: Department,
        sub_unit_hint: str | None = None,
    ) -> tuple[UnitHead, SubUnit | None]:
        profile = HIERARCHY.get(department)
        if profile is None:
            profile = HIERARCHY[Department.COMPLIANCE]

        unit_head = profile.unit_head
        sub_unit: SubUnit | None = None

        if sub_unit_hint:
            for su in profile.sub_units:
                if sub_unit_hint.lower() in su.name.lower():
                    sub_unit = su
                    break

        if sub_unit is None and profile.sub_units:
            sub_unit = profile.sub_units[0]

        return unit_head, sub_unit

    def build_raci(
        self,
        department: Department,
        unit_head: UnitHead,
        sub_unit: SubUnit | None,
    ) -> list[RACIEntry]:
        profile = HIERARCHY.get(department, HIERARCHY[Department.COMPLIANCE])
        raci: list[RACIEntry] = []

        # Accountable = Unit Head
        raci.append(RACIEntry(
            role=RACIRole.ACCOUNTABLE,
            party=unit_head.title,
            email=unit_head.email,
        ))

        # Responsible = Sub-unit lead (if any)
        if sub_unit:
            raci.append(RACIEntry(
                role=RACIRole.RESPONSIBLE,
                party=sub_unit.lead,
                email=sub_unit.email,
            ))

        # Consulted
        for c in profile.default_consulted:
            raci.append(RACIEntry(role=RACIRole.CONSULTED, party=c))

        # Informed
        for i in profile.default_informed:
            raci.append(RACIEntry(role=RACIRole.INFORMED, party=i))

        return raci


# ─────────────────────────────────────────────────────────────────────────────
# Cross-Functional Resolver
# ─────────────────────────────────────────────────────────────────────────────

class CrossFunctionalResolver:
    """
    When two or more departments are identified at ≥ threshold, generates
    one task per department and links them with a dependency graph.
    """

    def build_dependency_graph(
        self,
        tasks: list[RoutedTask],
        dept_order: list[Department],
    ) -> list[DependencyEdge]:
        """
        Simple sequential dependency: each task in dept_order blocks the next.
        Departments earlier in the list (higher confidence) are parents.
        """
        edges: list[DependencyEdge] = []
        for i in range(len(tasks) - 1):
            parent = tasks[i]
            child  = tasks[i + 1]
            edges.append(DependencyEdge(
                from_task_id=parent.task_id,
                to_task_id=child.task_id,
                relationship="BLOCKS",
                rationale=(
                    f"{parent.department.value} must complete its sub-tasks before "
                    f"{child.department.value} can proceed (cross-functional dependency)."
                ),
            ))
            # Mark child as BLOCKED
            child.status     = TaskStatus.BLOCKED
            child.depends_on = [parent.task_id]
            child.dependency_note = (
                f"Blocked pending completion of task {parent.task_id} "
                f"({parent.department.value} – {parent.unit_head.title})."
            )
        return edges


# ─────────────────────────────────────────────────────────────────────────────
# Exception Handler
# ─────────────────────────────────────────────────────────────────────────────

class ExceptionHandler:
    """
    Routes low-confidence results to the Compliance Officer as UNASSIGNED.
    """

    def __init__(self, hierarchy_resolver: HierarchyResolver) -> None:
        self._resolver = hierarchy_resolver

    def make_unassigned_task(
        self,
        obligation_id: str,
        directive_text: str,
        rule_confidence: float,
        ml_confidence: float,
    ) -> RoutedTask:
        dept = Department.COMPLIANCE
        unit_head, sub_unit = self._resolver.resolve(dept, "Policy & Governance")
        raci = self._resolver.build_raci(dept, unit_head, sub_unit)

        return RoutedTask(
            task_id=_new_id(),
            obligation_id=obligation_id,
            department=dept,
            unit_head=unit_head,
            sub_unit=sub_unit,
            directive_text=directive_text,
            routing_method="FALLBACK",
            rule_confidence=rule_confidence,
            ml_confidence=ml_confidence,
            final_confidence=max(rule_confidence, ml_confidence),
            status=TaskStatus.UNASSIGNED,
            raci=raci,
            depends_on=[],
            dependency_note=(
                "Routed UNASSIGNED to Compliance Officer due to low confidence "
                f"(rule={rule_confidence:.2f}, ml={ml_confidence:.2f}, "
                f"threshold={ROUTE_CONFIDENCE_THRESHOLD})."
            ),
            created_at=_now(),
        )


# ─────────────────────────────────────────────────────────────────────────────
# Hybrid Router (Orchestrator)
# ─────────────────────────────────────────────────────────────────────────────

class HybridRouter:
    """
    Entry point.  Combines RuleEngine + MLClassifier, resolves hierarchy,
    handles cross-functional splits, and enforces exception routing.

    Usage::

        router = HybridRouter()
        result = router.route(
            directive_text="All vendors MUST implement AES-256 encryption and pass KYC checks.",
            obligation_id="ob-001",
        )
        print(result.to_dict())
    """

    def __init__(self, correction_ledger: Optional[Any] = None) -> None:
        self._rule_engine    = RuleEngine()
        self._ml_classifier  = MLClassifier()
        self._hierarchy      = HierarchyResolver()
        self._cross_func     = CrossFunctionalResolver()
        self._exception_hdlr = ExceptionHandler(self._hierarchy)
        self._correction_ledger = correction_ledger

    # ── Public API ────────────────────────────────────────────────────────────

    def route(
        self,
        directive_text: str,
        obligation_id:  str | None = None,
    ) -> RoutingResult:
        """
        Route a single directive text.

        Returns:
            RoutingResult containing one or more tasks, a dependency graph,
            RACI definitions, and router metadata.
        """
        if not directive_text or not directive_text.strip():
            raise ValueError("directive_text must be a non-empty string.")

        obligation_id = obligation_id or _new_id()
        routing_id    = _new_id()
        notes: list[str] = []

        # ── Step 1: Rule Engine ───────────────────────────────────────────────
        rule_matches = self._rule_engine.classify(directive_text)
        top_rule     = rule_matches[0] if rule_matches else None
        rule_conf    = top_rule.confidence if top_rule else 0.0

        logger.info(
            "Rule engine: top=%s conf=%.4f",
            top_rule.department.value if top_rule else "none",
            rule_conf,
        )

        # ── Step 2: ML Classifier (fallback or supplementary) ─────────────────
        ml_results = self._ml_classifier.predict_proba(directive_text)
        top_ml     = ml_results[0] if ml_results else (Department.UNASSIGNED, 0.0)
        ml_conf    = top_ml[1]

        use_ml = rule_conf < RULE_CONFIDENCE_THRESHOLD
        routing_method = "ML" if use_ml else "RULE"

        if use_ml:
            notes.append(
                f"Rule confidence {rule_conf:.2f} < threshold {RULE_CONFIDENCE_THRESHOLD}; "
                "ML classifier used as primary."
            )
        else:
            notes.append(
                f"Rule engine primary (conf={rule_conf:.2f}); ML supplementary (conf={ml_conf:.2f})."
            )

        # ── Step 3: Cross-functional detection ───────────────────────────────
        # Collect top-K departments that both engines agree are significant
        candidate_depts = self._resolve_candidate_departments(
            rule_matches, ml_results, use_ml
        )

        is_cross_functional = len(candidate_depts) >= 2
        if is_cross_functional:
            notes.append(
                f"Cross-functional directive detected: "
                f"{[d.value for d, _ in candidate_depts[:CROSS_FUNCTIONAL_TOP_K]]}."
            )

        # ── Step 4: Confidence gate (UNASSIGNED) ─────────────────────────────
        best_conf = candidate_depts[0][1] if candidate_depts else 0.0
        if best_conf < ROUTE_CONFIDENCE_THRESHOLD:
            notes.append(
                f"Final confidence {best_conf:.2f} < {ROUTE_CONFIDENCE_THRESHOLD}; "
                "routing to Compliance Officer as UNASSIGNED."
            )
            unassigned_task = self._exception_hdlr.make_unassigned_task(
                obligation_id=obligation_id,
                directive_text=directive_text,
                rule_confidence=rule_conf,
                ml_confidence=ml_conf,
            )
            return RoutingResult(
                routing_id=routing_id,
                source_text=directive_text,
                obligation_id=obligation_id,
                tasks=[unassigned_task],
                dependency_graph=[],
                is_cross_functional=False,
                is_unassigned=True,
                router_notes=notes,
            )

        # ── Step 5: Build tasks ───────────────────────────────────────────────
        active_candidates = candidate_depts[:CROSS_FUNCTIONAL_TOP_K] if is_cross_functional \
                            else candidate_depts[:1]

        tasks: list[RoutedTask] = []
        for dept, conf in active_candidates:
            sub_hint = self._get_sub_unit_hint(dept, rule_matches, use_ml)
            unit_head, sub_unit = self._hierarchy.resolve(dept, sub_hint)
            raci = self._hierarchy.build_raci(dept, unit_head, sub_unit)

            task = RoutedTask(
                task_id=_new_id(),
                obligation_id=obligation_id,
                department=dept,
                unit_head=unit_head,
                sub_unit=sub_unit,
                directive_text=directive_text,
                routing_method=routing_method,
                rule_confidence=rule_conf,
                ml_confidence=ml_conf,
                final_confidence=round(conf, 4),
                status=TaskStatus.PENDING,
                raci=raci,
                depends_on=[],
                dependency_note=None,
                created_at=_now(),
            )
            tasks.append(task)

        # ── Step 6: Dependency graph for cross-functional tasks ───────────────
        dept_order = [dept for dept, _ in active_candidates]
        dep_graph  = self._cross_func.build_dependency_graph(tasks, dept_order) \
                     if is_cross_functional else []

        return RoutingResult(
            routing_id=routing_id,
            source_text=directive_text,
            obligation_id=obligation_id,
            tasks=tasks,
            dependency_graph=dep_graph,
            is_cross_functional=is_cross_functional,
            is_unassigned=False,
            router_notes=notes,
        )

    async def route_with_corrections(
        self,
        obligation_id: str,
        text: str,
        domain: str,
        regulation_ref: str,
    ) -> RoutedTask:
        """
        Route a single directive text with human-in-the-loop correction support.
        Queries the correction ledger; if a past CCO override matches the
        domain and regulation reference, uses the corrected department with
        a high confidence score.
        """
        corrections = []
        if self._correction_ledger:
            try:
                corrections = await self._correction_ledger.get_recent_corrections(
                    domain=domain, regulation_ref=regulation_ref
                )
            except Exception as exc:
                logger.error("Failed to query ledger corrections in router: %s", exc)

        if corrections:
            latest = corrections[0]
            corrected_val = latest.get("corrected_value", {})
            if isinstance(corrected_val, dict) and "department" in corrected_val:
                corrected_dept_str = corrected_val["department"]
                try:
                    dept = Department(corrected_dept_str)
                except ValueError:
                    dept = Department.COMPLIANCE

                unit_head, sub_unit = self._hierarchy.resolve(dept)
                raci = self._hierarchy.build_raci(dept, unit_head, sub_unit)

                task = RoutedTask(
                    task_id=_new_id(),
                    obligation_id=obligation_id,
                    department=dept,
                    unit_head=unit_head,
                    sub_unit=sub_unit,
                    directive_text=text,
                    routing_method="ledger_corrected",
                    rule_confidence=0.0,
                    ml_confidence=0.0,
                    final_confidence=0.95,
                    status=TaskStatus.PENDING,
                    raci=raci,
                    depends_on=[],
                    dependency_note=None,
                    created_at=_now(),
                )
                task.metadata = {"correction_id": latest.get("correction_id", "")}
                return task

        # Fallback to standard routing
        res = self.route(directive_text=text, obligation_id=obligation_id)
        if res.tasks:
            task = res.tasks[0]
            task.metadata = {}
            return task

        # Absolute fallback if no tasks
        dept = Department.COMPLIANCE
        unit_head, sub_unit = self._hierarchy.resolve(dept)
        raci = self._hierarchy.build_raci(dept, unit_head, sub_unit)
        task = RoutedTask(
            task_id=_new_id(),
            obligation_id=obligation_id,
            department=dept,
            unit_head=unit_head,
            sub_unit=sub_unit,
            directive_text=text,
            routing_method="FALLBACK",
            rule_confidence=0.0,
            ml_confidence=0.0,
            final_confidence=0.50,
            status=TaskStatus.PENDING,
            raci=raci,
            depends_on=[],
            dependency_note=None,
            created_at=_now(),
        )
        task.metadata = {}
        return task

    def route_batch(
        self,
        directives: list[dict[str, str]],
    ) -> list[RoutingResult]:
        """
        Route a list of directives.

        Args:
            directives: list of dicts with 'text' (required) and 'obligation_id' (optional).

        Returns:
            list of RoutingResult, one per directive.
        """
        results = []
        for item in directives:
            results.append(self.route(
                directive_text=item.get("text", ""),
                obligation_id=item.get("obligation_id"),
            ))
        return results

    # ── Private helpers ───────────────────────────────────────────────────────

    def _resolve_candidate_departments(
        self,
        rule_matches: list[RuleMatch],
        ml_results:   list[tuple[Department, float]],
        use_ml:        bool,
    ) -> list[tuple[Department, float]]:
        """
        Merge rule and ML signals into a ranked list of (Department, score).
        Rule and ML scores are blended; cross-functional candidates kept if
        both their scores exceed the route threshold.
        """
        scores: dict[Department, float] = {}

        # Rule engine scores (weight 0.6 if primary, 0.4 if supplementary)
        rule_weight = 0.4 if use_ml else 0.6
        for rm in rule_matches:
            scores[rm.department] = scores.get(rm.department, 0.0) + rm.confidence * rule_weight

        # ML scores (weight 0.6 if primary, 0.4 if supplementary)
        ml_weight = 0.6 if use_ml else 0.4
        for dept, prob in ml_results:
            scores[dept] = scores.get(dept, 0.0) + prob * ml_weight

        # Cap at 0.99
        scores = {d: min(0.99, s) for d, s in scores.items()}

        ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
        return ranked

    def _get_sub_unit_hint(
        self,
        dept:         Department,
        rule_matches: list[RuleMatch],
        use_ml:       bool,
    ) -> str | None:
        """Return sub_unit_hint from the best rule match for this department."""
        for rm in rule_matches:
            if rm.department == dept and rm.sub_unit_hint:
                return rm.sub_unit_hint
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Utility helpers
# ─────────────────────────────────────────────────────────────────────────────

def _new_id() -> str:
    return str(uuid.uuid4())


def _now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


# ─────────────────────────────────────────────────────────────────────────────
# Module-level singleton (optional convenience)
# ─────────────────────────────────────────────────────────────────────────────

_DEFAULT_ROUTER: HybridRouter | None = None


def get_router() -> HybridRouter:
    """Return (or lazily create) the module-level singleton router."""
    global _DEFAULT_ROUTER
    if _DEFAULT_ROUTER is None:
        _DEFAULT_ROUTER = HybridRouter()
    return _DEFAULT_ROUTER


def route(directive_text: str, obligation_id: str | None = None) -> dict[str, Any]:
    """
    Convenience wrapper.  Routes a directive and returns the serialisable dict.

    Example::

        from routing.router import route
        result = route("Vendors MUST implement AES-256 encryption and pass KYC checks.")
    """
    return get_router().route(directive_text, obligation_id).to_dict()


# ─────────────────────────────────────────────────────────────────────────────
# CLI demo
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import json

    DEMO_DIRECTIVES = [
        {
            "obligation_id": "ob-001",
            "text": (
                "All vendors MUST implement AES-256 encryption and complete "
                "KYC onboarding checks within 15 days of contract signature."
            ),
        },
        {
            "obligation_id": "ob-002",
            "text": (
                "The Treasury team SHALL review liquidity coverage ratios quarterly "
                "and report any breach to the Asset-Liability Management committee."
            ),
        },
        {
            "obligation_id": "ob-003",
            "text": (
                "Branches SHOULD update customer due diligence records annually."
            ),
        },
        {
            "obligation_id": "ob-004",
            "text": (
                "The organisation must ensure robust data governance."  # intentionally vague
            ),
        },
    ]

    router = HybridRouter()
    results = router.route_batch(DEMO_DIRECTIVES)

    for res in results:
        print(json.dumps(res.to_dict(), indent=2))
        print("─" * 80)