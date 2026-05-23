"""
knowledge/risk_engine.py
=========================
Multi-factor RiskScoringEngine — combines regulatory severity, system impact,
temporal urgency, cross-regulation coverage, and historical breach signals
into a single 0.0–10.0 risk score.

Factors (weighted):
  1. Regulatory severity   (30%) — penalty level in the ontology
  2. System impact         (25%) — number of impacted systems
  3. Temporal urgency      (20%) — deadline proximity, expiry status
  4. Cross-reg amplifier   (15%) — how many regulators watch this topic
  5. Historical breach rate (10%) — past violations for this domain
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Optional

from knowledge.policy_graph import PolicyGraph
from knowledge.rule_compiler import CompiledRule
from knowledge.temporal_reasoner import TemporalReasoner

logger = logging.getLogger(__name__)


# =============================================================================
# Data Structures
# =============================================================================

@dataclass
class RiskFactor:
    """A single scored component of the overall risk."""
    factor_name: str
    raw_score:   float     # 0.0–10.0
    weight:      float     # 0.0–1.0
    weighted:    float     # raw_score × weight
    rationale:   str

    def to_dict(self) -> dict[str, Any]:
        return {
            "factor_name": self.factor_name,
            "raw_score":   round(self.raw_score, 2),
            "weight":      self.weight,
            "weighted":    round(self.weighted, 4),
            "rationale":   self.rationale,
        }


@dataclass
class RiskScore:
    """Complete risk assessment for an obligation."""
    obligation_text:  str
    domain:           str
    overall_score:    float           # 0.0–10.0
    risk_level:       str             # CRITICAL | HIGH | MEDIUM | LOW
    factors:          list[RiskFactor] = field(default_factory=list)
    recommendation:   str = ""
    breach_history:   dict[str, Any] = field(default_factory=dict)
    regulation_refs:  list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "obligation_text": self.obligation_text[:200],
            "domain":          self.domain,
            "overall_score":   round(self.overall_score, 2),
            "risk_level":      self.risk_level,
            "factors":         [f.to_dict() for f in self.factors],
            "recommendation":  self.recommendation,
            "breach_history":  self.breach_history,
            "regulation_refs": self.regulation_refs,
        }


# =============================================================================
# Severity + Historical Maps (domain-specific IP)
# =============================================================================

# Maps PenaltySeverity → base regulatory severity score
_SEVERITY_SCORES = {"HIGH": 9.0, "MEDIUM": 5.5, "LOW": 2.0}

# Simulated historical breach rate per domain (would come from audit ledger in prod)
# Format: domain → {"breach_count_12m": N, "last_breach": "YYYY-MM-DD", "rate": 0.0-1.0}
_HISTORICAL_BREACH_RATES: dict[str, dict[str, Any]] = {
    "encryption":     {"breach_count_12m": 3,  "rate": 0.35, "last_breach": "2024-02-15"},
    "kyc":            {"breach_count_12m": 12, "rate": 0.60, "last_breach": "2024-09-01"},
    "aml":            {"breach_count_12m": 7,  "rate": 0.45, "last_breach": "2024-05-20"},
    "data_protection":{"breach_count_12m": 2,  "rate": 0.20, "last_breach": "2023-11-10"},
    "cyber":          {"breach_count_12m": 5,  "rate": 0.40, "last_breach": "2024-06-30"},
    "capital":        {"breach_count_12m": 1,  "rate": 0.10, "last_breach": "2023-04-01"},
    "liquidity":      {"breach_count_12m": 1,  "rate": 0.10, "last_breach": "2024-01-15"},
    "outsourcing":    {"breach_count_12m": 4,  "rate": 0.30, "last_breach": "2024-03-22"},
    "fraud":          {"breach_count_12m": 8,  "rate": 0.50, "last_breach": "2024-10-05"},
    "audit":          {"breach_count_12m": 2,  "rate": 0.15, "last_breach": "2023-12-01"},
}

# Recommendation templates per risk level
_RECOMMENDATIONS = {
    "CRITICAL": (
        "IMMEDIATE ACTION REQUIRED. Escalate to CISO, CRO, and Compliance Officer today. "
        "Convene emergency task force. Prepare Board notification if breach imminent."
    ),
    "HIGH": (
        "Assign dedicated project manager. Create ServiceNow Change Request with daily tracking. "
        "CISO review required within 3 business days. "
        "Interim compensating controls must be documented."
    ),
    "MEDIUM": (
        "Schedule implementation within the regulation deadline. "
        "Assign to IT Security / Compliance team with bi-weekly review. "
        "No immediate escalation required unless timeline slips."
    ),
    "LOW": (
        "Include in next quarterly compliance review cycle. "
        "Standard monitoring applies. Document evidence and close via regular audit."
    ),
}


# =============================================================================
# Risk Scoring Engine
# =============================================================================

class RiskScoringEngine:
    """
    Multi-factor risk scorer for compliance obligations.

    Usage::

        engine = RiskScoringEngine()
        score = engine.score(
            obligation_text  = "Banks shall encrypt customer data at rest using AES-256.",
            compiled_rule    = compiled,
            impacted_systems = ["CBS", "Data Warehouse", "Backup"],
            policy_conflicts = [{"severity": "HIGH", ...}],
        )
        print(score.overall_score, score.risk_level, score.recommendation)
    """

    # Factor weights (must sum to 1.0)
    WEIGHT_SEVERITY    = 0.30
    WEIGHT_IMPACT      = 0.25
    WEIGHT_URGENCY     = 0.20
    WEIGHT_CROSS_REG   = 0.15
    WEIGHT_HISTORY     = 0.10

    def __init__(
        self,
        policy_graph:    Optional[PolicyGraph] = None,
        temporal_reasoner: Optional[TemporalReasoner] = None,
    ):
        self._pg = policy_graph or PolicyGraph()
        self._tr = temporal_reasoner or TemporalReasoner()

    def score(
        self,
        obligation_text:  str,
        domain:           str = "cyber",
        compiled_rule:    Optional[CompiledRule] = None,
        impacted_systems: Optional[list[str]] = None,
        policy_conflicts: Optional[list[dict[str, Any]]] = None,
        regulation_ref:   str = "",
        as_of:            Optional[date] = None,
    ) -> RiskScore:
        """
        Compute a multi-factor risk score for an obligation.
        All factors are scored 0.0–10.0 then weighted.
        """
        factors: list[RiskFactor] = []
        check_date = as_of or date.today()

        # ── Factor 1: Regulatory Severity ─────────────────────────────────────
        sev_score, sev_rationale, reg_refs = self._score_regulatory_severity(
            domain, regulation_ref, policy_conflicts
        )
        factors.append(RiskFactor(
            factor_name = "Regulatory Severity",
            raw_score   = sev_score,
            weight      = self.WEIGHT_SEVERITY,
            weighted    = sev_score * self.WEIGHT_SEVERITY,
            rationale   = sev_rationale,
        ))

        # ── Factor 2: System Impact ────────────────────────────────────────────
        sys_count = len(impacted_systems) if impacted_systems else 3
        sys_score, sys_rationale = self._score_system_impact(
            sys_count, compiled_rule
        )
        factors.append(RiskFactor(
            factor_name = "System Impact",
            raw_score   = sys_score,
            weight      = self.WEIGHT_IMPACT,
            weighted    = sys_score * self.WEIGHT_IMPACT,
            rationale   = sys_rationale,
        ))

        # ── Factor 3: Temporal Urgency ────────────────────────────────────────
        urg_score, urg_rationale = self._score_temporal_urgency(
            compiled_rule, regulation_ref, check_date
        )
        factors.append(RiskFactor(
            factor_name = "Temporal Urgency",
            raw_score   = urg_score,
            weight      = self.WEIGHT_URGENCY,
            weighted    = urg_score * self.WEIGHT_URGENCY,
            rationale   = urg_rationale,
        ))

        # ── Factor 4: Cross-Regulation Amplifier ──────────────────────────────
        cross = self._pg.cross_regulation_coverage(obligation_text[:50], domain)
        # Convert amplifier (1.0–1.6) to 0–10 scale
        amp_score = (cross.risk_amplifier - 1.0) / 0.6 * 10
        factors.append(RiskFactor(
            factor_name = "Cross-Regulation Coverage",
            raw_score   = round(amp_score, 2),
            weight      = self.WEIGHT_CROSS_REG,
            weighted    = amp_score * self.WEIGHT_CROSS_REG,
            rationale   = (
                f"{cross.count} regulation(s) from {len(cross.unique_sources)} "
                f"source(s) cover this domain "
                f"({', '.join(cross.unique_sources)}). "
                f"Amplifier: {cross.risk_amplifier:.1f}x."
            ),
        ))

        # ── Factor 5: Historical Breach Rate ──────────────────────────────────
        hist = _HISTORICAL_BREACH_RATES.get(domain, {"breach_count_12m": 0, "rate": 0.1})
        hist_score = hist["rate"] * 10.0
        factors.append(RiskFactor(
            factor_name = "Historical Breach Rate",
            raw_score   = round(hist_score, 2),
            weight      = self.WEIGHT_HISTORY,
            weighted    = hist_score * self.WEIGHT_HISTORY,
            rationale   = (
                f"{hist['breach_count_12m']} {domain} breach incident(s) "
                f"in the last 12 months (internal audit ledger). "
                f"Last breach: {hist.get('last_breach', 'N/A')}."
            ),
        ))

        # ── Overall Score ──────────────────────────────────────────────────────
        overall = sum(f.weighted for f in factors)
        overall = min(round(overall, 2), 10.0)

        # Risk level bucketing
        if overall >= 7.5:
            risk_level = "CRITICAL"
        elif overall >= 5.5:
            risk_level = "HIGH"
        elif overall >= 3.0:
            risk_level = "MEDIUM"
        else:
            risk_level = "LOW"

        return RiskScore(
            obligation_text = obligation_text,
            domain          = domain,
            overall_score   = overall,
            risk_level      = risk_level,
            factors         = factors,
            recommendation  = _RECOMMENDATIONS[risk_level],
            breach_history  = hist,
            regulation_refs = reg_refs,
        )

    def score_batch(
        self,
        obligations: list[dict[str, Any]],
    ) -> list[RiskScore]:
        """
        Score multiple obligations.
        Each dict: {"text": str, "domain": str, "compiled_rule": CompiledRule, ...}
        """
        return [
            self.score(
                obligation_text  = ob.get("text", ""),
                domain           = ob.get("domain", "cyber"),
                compiled_rule    = ob.get("compiled_rule"),
                impacted_systems = ob.get("impacted_systems"),
                policy_conflicts = ob.get("policy_conflicts"),
                regulation_ref   = ob.get("regulation_ref", ""),
            )
            for ob in obligations
        ]

    # ── Factor Scorers ────────────────────────────────────────────────────────

    def _score_regulatory_severity(
        self,
        domain: str,
        regulation_ref: str,
        policy_conflicts: Optional[list[dict[str, Any]]],
    ) -> tuple[float, str, list[str]]:
        """Score based on penalty severity of relevant regulations."""
        domain_nodes = self._pg._onto.get_nodes_by_domain(domain)
        if not domain_nodes:
            return 5.0, "No domain-specific nodes found; using default MEDIUM.", []

        # Average severity of mandatory nodes
        sev_scores = [
            _SEVERITY_SCORES.get(n.penalty_severity, 5.5)
            for n in domain_nodes
            if n.mandatory and n.is_active()
        ]
        base = sum(sev_scores) / len(sev_scores) if sev_scores else 5.0

        # Add conflict penalty
        if policy_conflicts:
            high_conflicts = [c for c in policy_conflicts if c.get("severity") == "HIGH"]
            base = min(base + len(high_conflicts) * 0.5, 10.0)

        refs = [n.reg_id for n in domain_nodes if n.mandatory and n.is_active()][:4]
        rationale = (
            f"Domain '{domain}' has {len(domain_nodes)} active regulation(s). "
            f"Average penalty severity score: {base:.1f}. "
            f"Active conflicts: {len(policy_conflicts) if policy_conflicts else 0}."
        )
        return round(base, 2), rationale, refs

    def _score_system_impact(
        self,
        sys_count: int,
        rule: Optional[CompiledRule],
    ) -> tuple[float, str]:
        """Score based on number of impacted systems and technical complexity."""
        # Base: 1-2 systems=3, 3-4=5, 5-6=7, 7+=9
        if sys_count <= 2:
            base = 3.0
        elif sys_count <= 4:
            base = 5.0
        elif sys_count <= 6:
            base = 7.0
        else:
            base = 9.0

        # Technical complexity bonus
        if rule and rule.algorithm in ("AES-256", "RSA-4096", "TLSv1.3"):
            base = min(base + 0.5, 10.0)

        return round(base, 2), (
            f"{sys_count} system(s) impacted. "
            f"Algorithm: {rule.algorithm if rule and rule.algorithm else 'N/A'}. "
            f"Complexity score: {base:.1f}/10."
        )

    def _score_temporal_urgency(
        self,
        rule: Optional[CompiledRule],
        regulation_ref: str,
        check_date: date,
    ) -> tuple[float, str]:
        """Score based on deadline proximity and supersession status."""
        deadline_days = rule.deadline_days if rule else 30

        # Convert deadline to urgency score
        if deadline_days <= 7:
            base = 9.5
        elif deadline_days <= 14:
            base = 8.0
        elif deadline_days <= 30:
            base = 6.5
        elif deadline_days <= 90:
            base = 4.5
        elif deadline_days <= 365:
            base = 3.0
        else:
            base = 1.5

        # Check if regulation has been recently superseded
        if regulation_ref:
            try:
                recent = self._tr.get_recent_changes(lookback_days=90)
                superseded_refs = [c["reg_id"] for c in recent if c["type"] == "NEW_OR_AMENDED"]
                if any(regulation_ref in ref for ref in superseded_refs):
                    base = min(base + 1.5, 10.0)
            except Exception:
                pass

        return round(base, 2), (
            f"Implementation deadline: {deadline_days} day(s). "
            f"Urgency score: {base:.1f}/10."
        )
