"""
agents/verifier_agent.py
=========================
VerifierAgent — Detects linguistic ambiguity in regulatory obligations.

Checks:
  1. Modal verb strength  ("shall" vs "should" vs "may")
  2. Missing subject      ("banks shall" — which banks? commercial? NBFC?)
  3. Quantitative vagueness ("adequate" security — adequate by whose standard?)
  4. Temporal vagueness   ("promptly" — how many hours/days?)
  5. Scope ambiguity      ("all data" — structured only? unstructured too?)
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


# =============================================================================
# Data Structures
# =============================================================================

@dataclass
class AmbiguityFlag:
    """A single identified ambiguity in an obligation."""
    flag_type:     str   # MODAL | SUBJECT | QUANTITATIVE | TEMPORAL | SCOPE
    severity:      str   # HIGH | MEDIUM | LOW
    excerpt:       str   # the ambiguous text fragment
    explanation:   str
    suggestion:    str   # how to clarify

    def to_dict(self) -> dict[str, Any]:
        return {
            "flag_type":   self.flag_type,
            "severity":    self.severity,
            "excerpt":     self.excerpt,
            "explanation": self.explanation,
            "suggestion":  self.suggestion,
        }


@dataclass
class AmbiguityReport:
    """Full ambiguity analysis for an obligation."""
    obligation_text:     str
    ambiguity_score:     float    # 0.0 = crystal-clear, 1.0 = completely vague
    flags:               list[AmbiguityFlag] = field(default_factory=list)
    unclear_terms:       list[str] = field(default_factory=list)
    clarification_needed: bool = False
    summary:             str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "obligation_text":      self.obligation_text[:200],
            "ambiguity_score":      round(self.ambiguity_score, 4),
            "flags":                [f.to_dict() for f in self.flags],
            "unclear_terms":        self.unclear_terms,
            "clarification_needed": self.clarification_needed,
            "summary":              self.summary,
        }


# =============================================================================
# Pattern Libraries
# =============================================================================

# Modal verb strength (HIGH = binding, LOW = suggestive)
_MODAL_STRENGTH: list[tuple[re.Pattern, str, str]] = [
    (re.compile(r'\b(?:shall|must)\b', re.I),               "HIGH",   "Binding obligation"),
    (re.compile(r'\b(?:required\s+to|directed\s+to|mandated)\b', re.I), "HIGH", "Binding directive"),
    (re.compile(r'\bshould\b', re.I),                       "MEDIUM", "Recommended but not strictly mandatory"),
    (re.compile(r'\b(?:may|can|could)\b', re.I),            "LOW",    "Permissive — optional"),
    (re.compile(r'\b(?:is\s+encouraged|is\s+advisable)\b', re.I), "LOW", "Advisory only"),
]

# Vague quantifiers that need precise definition
_VAGUE_QUANTIFIERS: list[tuple[re.Pattern, str]] = [
    (re.compile(r'\badequate\b', re.I),          "adequate"),
    (re.compile(r'\bsufficient\b', re.I),        "sufficient"),
    (re.compile(r'\bappropriate\b', re.I),       "appropriate"),
    (re.compile(r'\breasonable\b', re.I),        "reasonable"),
    (re.compile(r'\brobust\b', re.I),            "robust"),
    (re.compile(r'\bstrong\b', re.I),            "strong"),
    (re.compile(r'\beffective\b', re.I),         "effective"),
    (re.compile(r'\bsecure\b', re.I),            "secure"),
    (re.compile(r'\bproper\b', re.I),            "proper"),
    (re.compile(r'\bsatisfactory\b', re.I),      "satisfactory"),
    (re.compile(r'\bregular(?:ly)?\b', re.I),   "regularly"),
    (re.compile(r'\bperiodic(?:ally)?\b', re.I), "periodically"),
    (re.compile(r'\bpromptly\b', re.I),          "promptly"),
    (re.compile(r'\bimmediately\b', re.I),       "immediately"),
    (re.compile(r'\bforthwith\b', re.I),         "forthwith"),
]

# Vague scope terms
_VAGUE_SCOPE: list[tuple[re.Pattern, str, str]] = [
    (re.compile(r'\ball\s+data\b', re.I),         "all data",         "Does this include unstructured data, logs, and backups?"),
    (re.compile(r'\bsensitive\s+(?:data|information)\b', re.I), "sensitive data", "What constitutes 'sensitive'? PII only, or financial data too?"),
    (re.compile(r'\bcritical\s+(?:systems?|infrastructure)\b', re.I), "critical systems", "Which specific systems qualify as 'critical'?"),
    (re.compile(r'\bpersonnel\b|\bstaff\b|\bemployees?\b', re.I), "staff", "All employees or only IT/compliance roles?"),
    (re.compile(r'\bthird.?part(?:y|ies)\b|\bvendors?\b', re.I), "third parties", "All vendors or only critical/IT vendors?"),
    (re.compile(r'\blegacy\s+systems?\b', re.I),  "legacy systems",   "No timeline or definition provided for legacy classification"),
]

# Subject ambiguity — when no clear subject is stated
_SUBJECT_INDICATORS = re.compile(
    r'\b(?:banks?|nbf[cs]?|entities|organisations?|companies|'
    r'regulated\s+entities?|service\s+providers?|intermediaries)\b',
    re.I,
)

# Temporal vagueness
_TEMPORAL_VAGUE: list[tuple[re.Pattern, str, str]] = [
    (re.compile(r'\bpromptly\b', re.I),           "promptly",           "Define as a specific timeframe (e.g. within 6 hours)"),
    (re.compile(r'\bimmediately\b', re.I),         "immediately",        "Define as a specific timeframe (e.g. within 1 hour)"),
    (re.compile(r'\bregularly\b', re.I),           "regularly",          "Specify frequency (e.g. monthly, quarterly, annually)"),
    (re.compile(r'\bperiodically\b', re.I),        "periodically",       "Specify frequency (e.g. every 6 months)"),
    (re.compile(r'\bas\s+soon\s+as\s+possible\b', re.I), "as soon as possible", "Replace with a binding timeframe"),
    (re.compile(r'\bin\s+due\s+course\b', re.I),  "in due course",      "This is unenforceable — specify a deadline"),
]


# =============================================================================
# Verifier Agent
# =============================================================================

class VerifierAgent:
    """
    Detects linguistic ambiguity in regulatory obligation text.

    Usage::

        agent = VerifierAgent()
        report = agent.verify("Banks should implement adequate security measures.")
        print(report.ambiguity_score, report.flags)
    """

    def verify(self, obligation_text: str) -> AmbiguityReport:
        """Run all ambiguity checks on an obligation text."""
        text = obligation_text.strip()
        flags: list[AmbiguityFlag] = []
        unclear_terms: list[str] = []

        # Check 1: Modal verb
        modal_flags = self._check_modal_verbs(text)
        flags.extend(modal_flags)

        # Check 2: Subject clarity
        subject_flags = self._check_subject(text)
        flags.extend(subject_flags)

        # Check 3: Vague quantifiers
        quant_flags, quant_terms = self._check_vague_quantifiers(text)
        flags.extend(quant_flags)
        unclear_terms.extend(quant_terms)

        # Check 4: Temporal vagueness
        temporal_flags, temporal_terms = self._check_temporal_vagueness(text)
        flags.extend(temporal_flags)
        unclear_terms.extend(temporal_terms)

        # Check 5: Scope ambiguity
        scope_flags, scope_terms = self._check_scope_ambiguity(text)
        flags.extend(scope_flags)
        unclear_terms.extend(scope_terms)

        # Score: weighted average of flag severities
        score = self._compute_score(flags)
        clarification_needed = score > 0.45 or any(f.severity == "HIGH" for f in flags)

        summary_parts: list[str] = []
        if flags:
            high = [f for f in flags if f.severity == "HIGH"]
            med  = [f for f in flags if f.severity == "MEDIUM"]
            if high:
                summary_parts.append(f"{len(high)} HIGH-severity ambiguit(ies): " + "; ".join(f.flag_type for f in high))
            if med:
                summary_parts.append(f"{len(med)} MEDIUM: " + "; ".join(f.flag_type for f in med))
        summary = " | ".join(summary_parts) if summary_parts else "Obligation is sufficiently precise."

        return AmbiguityReport(
            obligation_text      = text,
            ambiguity_score      = score,
            flags                = flags,
            unclear_terms        = list(set(unclear_terms)),
            clarification_needed = clarification_needed,
            summary              = summary,
        )

    def verify_batch(self, obligations: list[str]) -> list[AmbiguityReport]:
        return [self.verify(ob) for ob in obligations]

    # ── Check Methods ────────────────────────────────────────────────────────

    def _check_modal_verbs(self, text: str) -> list[AmbiguityFlag]:
        flags: list[AmbiguityFlag] = []
        for pattern, strength, label in _MODAL_STRENGTH:
            match = pattern.search(text)
            if match and strength in ("LOW", "MEDIUM"):
                flags.append(AmbiguityFlag(
                    flag_type   = "MODAL",
                    severity    = "HIGH" if strength == "LOW" else "MEDIUM",
                    excerpt     = match.group(),
                    explanation = f"Modal verb '{match.group()}' is {label}. This weakens enforceability.",
                    suggestion  = f"Replace '{match.group()}' with 'shall' or 'must' if this is a binding requirement.",
                ))
        return flags

    def _check_subject(self, text: str) -> list[AmbiguityFlag]:
        if not _SUBJECT_INDICATORS.search(text):
            return [AmbiguityFlag(
                flag_type   = "SUBJECT",
                severity    = "MEDIUM",
                excerpt     = text[:60],
                explanation = "No clear subject identified. Who exactly does this obligation apply to?",
                suggestion  = "Explicitly state the regulated entity: 'All Scheduled Commercial Banks shall...'",
            )]
        return []

    def _check_vague_quantifiers(self, text: str) -> tuple[list[AmbiguityFlag], list[str]]:
        flags: list[AmbiguityFlag] = []
        terms: list[str] = []
        for pattern, term in _VAGUE_QUANTIFIERS:
            if pattern.search(text):
                terms.append(term)
                flags.append(AmbiguityFlag(
                    flag_type   = "QUANTITATIVE",
                    severity    = "MEDIUM",
                    excerpt     = term,
                    explanation = f"'{term}' is subjective and not measurable without further definition.",
                    suggestion  = f"Replace '{term}' with a specific, measurable standard (e.g. 'AES-256', 'within 6 hours').",
                ))
        return flags, terms

    def _check_temporal_vagueness(self, text: str) -> tuple[list[AmbiguityFlag], list[str]]:
        flags: list[AmbiguityFlag] = []
        terms: list[str] = []
        for pattern, term, suggestion in _TEMPORAL_VAGUE:
            if pattern.search(text):
                terms.append(term)
                flags.append(AmbiguityFlag(
                    flag_type   = "TEMPORAL",
                    severity    = "HIGH",
                    excerpt     = term,
                    explanation = f"'{term}' is temporally vague and unenforceable as-is.",
                    suggestion  = suggestion,
                ))
        return flags, terms

    def _check_scope_ambiguity(self, text: str) -> tuple[list[AmbiguityFlag], list[str]]:
        flags: list[AmbiguityFlag] = []
        terms: list[str] = []
        for pattern, term, detail in _VAGUE_SCOPE:
            if pattern.search(text):
                terms.append(term)
                flags.append(AmbiguityFlag(
                    flag_type   = "SCOPE",
                    severity    = "MEDIUM",
                    excerpt     = term,
                    explanation = f"Scope of '{term}' is undefined. {detail}",
                    suggestion  = f"Define the precise scope of '{term}' with reference to data classification policy.",
                ))
        return flags, terms

    @staticmethod
    def _compute_score(flags: list[AmbiguityFlag]) -> float:
        if not flags:
            return 0.0
        weights = {"HIGH": 0.30, "MEDIUM": 0.15, "LOW": 0.05}
        total = sum(weights.get(f.severity, 0.10) for f in flags)
        return min(total, 1.0)
