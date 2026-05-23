"""
agents/critic_agent.py
=======================
CriticAgent — detects policy conflicts, identifies supersessions, and finds
regulatory precedents using the ComplianceOntology.

Checks:
  1. Direct ontology CONFLICTS_WITH edges for the obligation's domain
  2. Supersession status — is the cited regulation still current?
  3. Cross-regulation coverage amplifier (how many regulators watch this)
  4. Dependency chain completeness (are prerequisite obligations satisfied?)
  5. Encryption/technical standard downgrades (AES-128 → AES-256 conflict)
"""
from __future__ import annotations

import difflib
import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Optional

from knowledge.policy_graph import PolicyGraph, PolicyTensionReport
from knowledge.rule_compiler import RuleCompiler, CompiledRule
from knowledge.temporal_reasoner import TemporalReasoner

logger = logging.getLogger(__name__)


# =============================================================================
# Data Structures
# =============================================================================

@dataclass
class ConflictDetail:
    """A single detected conflict."""
    conflict_type:   str   # DIRECT | SUPERSESSION | STANDARD_DOWNGRADE | DEPENDENCY_GAP
    severity:        str   # HIGH | MEDIUM | LOW
    regulation_a:    str   # reg_id of first party
    regulation_b:    str   # reg_id of conflicting party
    description:     str
    resolution:      str   # suggested resolution
    precedent_refs:  list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "conflict_type":  self.conflict_type,
            "severity":       self.severity,
            "regulation_a":   self.regulation_a,
            "regulation_b":   self.regulation_b,
            "description":    self.description,
            "resolution":     self.resolution,
            "precedent_refs": self.precedent_refs,
        }


@dataclass
class CritiqueReport:
    """Full critique output for an obligation."""
    obligation_text:   str
    regulation_ref:    str
    conflicts:         list[ConflictDetail] = field(default_factory=list)
    supersessions:     list[dict[str, Any]] = field(default_factory=list)
    precedent_refs:    list[str] = field(default_factory=list)
    dependency_gaps:   list[str] = field(default_factory=list)
    cross_reg_count:   int = 0
    risk_amplifier:    float = 1.0
    critique_passed:   bool = True  # False = at least one HIGH conflict found
    summary:           str = ""
    # Phase 9 — Feature 2: Tension detection
    tension_report:    Optional[PolicyTensionReport] = None
    # Phase 9 — Feature 4: Anti-hallucination + evidence hardening
    hallucination_detected: bool = False
    hallucination_score:    float = 1.0   # 1.0 = exact match, 0.0 = not found
    evidence_flags:    list[dict[str, Any]] = field(default_factory=list)  # rejected evidence items

    def to_dict(self) -> dict[str, Any]:
        d = {
            "obligation_text":      self.obligation_text[:200],
            "regulation_ref":       self.regulation_ref,
            "conflicts":            [c.to_dict() for c in self.conflicts],
            "supersessions":        self.supersessions,
            "precedent_refs":       self.precedent_refs,
            "dependency_gaps":      self.dependency_gaps,
            "cross_reg_count":      self.cross_reg_count,
            "risk_amplifier":       round(self.risk_amplifier, 3),
            "critique_passed":      self.critique_passed,
            "summary":              self.summary,
            "hallucination_detected": self.hallucination_detected,
            "hallucination_score":  round(self.hallucination_score, 4),
            "evidence_flags":       self.evidence_flags,
        }
        if self.tension_report:
            d["tension_report"] = self.tension_report.to_dict()
        return d


# =============================================================================
# Critic Agent
# =============================================================================

class CriticAgent:
    """
    Detects policy conflicts, supersession issues, and regulatory precedents.

    Usage::

        agent = CriticAgent()
        report = agent.critique(
            obligation_text="Banks shall use AES-128 encryption for data at rest.",
            regulation_ref="rbi_it_framework_2019",
            domain="encryption",
        )
        # → flags STANDARD_DOWNGRADE conflict (AES-128 vs AES-256 in 2023 framework)
    """

    def __init__(
        self,
        policy_graph: Optional[PolicyGraph] = None,
        rule_compiler: Optional[RuleCompiler] = None,
        temporal_reasoner: Optional[TemporalReasoner] = None,
    ):
        self._pg = policy_graph or PolicyGraph()
        self._rc = rule_compiler or RuleCompiler()
        self._tr = temporal_reasoner or TemporalReasoner()

    def critique(
        self,
        obligation_text: str,
        regulation_ref: str = "",
        domain: str = "",
        as_of: Optional[date] = None,
    ) -> CritiqueReport:
        """
        Run full critique on an obligation.

        Returns CritiqueReport with all conflicts, supersessions,
        and dependency gaps identified.
        """
        compiled = self._rc.compile(obligation_text)
        effective_domain = domain or compiled.domain or "cyber"

        report = CritiqueReport(
            obligation_text = obligation_text,
            regulation_ref  = regulation_ref,
        )

        # Check 1: Direct ontology conflicts
        direct_conflicts = self._check_direct_conflicts(
            obligation_text, effective_domain, regulation_ref
        )
        report.conflicts.extend(direct_conflicts)

        # Check 2: Supersession — is cited regulation still current?
        if regulation_ref:
            sup_conflicts, sup_info = self._check_supersession(regulation_ref, as_of)
            report.conflicts.extend(sup_conflicts)
            report.supersessions = sup_info

        # Check 3: Technical standard downgrade detection
        std_conflicts = self._check_standard_downgrade(compiled, effective_domain)
        report.conflicts.extend(std_conflicts)

        # Check 4: Dependency gaps
        if regulation_ref:
            report.dependency_gaps = self._check_dependency_gaps(regulation_ref)

        # Check 5: Cross-regulation coverage
        cross = self._pg.cross_regulation_coverage(obligation_text[:50], effective_domain)
        report.cross_reg_count = cross.count
        report.risk_amplifier  = cross.risk_amplifier

        # Collect precedent references
        report.precedent_refs = self._collect_precedents(effective_domain)

        # Phase 9 - Feature 2: Policy tension detection
        report.tension_report = self._check_policy_tension(
            obligation_text, regulation_ref, effective_domain
        )
        if report.tension_report and report.tension_report.has_tension:
            for pair in report.tension_report.tension_pairs:
                report.conflicts.append(ConflictDetail(
                    conflict_type = "POLICY_TENSION",
                    severity      = "MEDIUM",
                    regulation_a  = pair.regulation_a,
                    regulation_b  = pair.regulation_b,
                    description   = (
                        f"Semantic tension: '{pair.verb_a}' vs '{pair.verb_b}' "
                        f"on shared object '{pair.shared_object}'."
                    ),
                    resolution    = (
                        report.tension_report.compensating_controls[0]
                        if report.tension_report.compensating_controls
                        else "Consult Legal/Compliance to resolve tension."
                    ),
                ))

        # Overall pass/fail
        report.critique_passed = not any(c.severity == "HIGH" for c in report.conflicts)

        # Build summary
        high_count = sum(1 for c in report.conflicts if c.severity == "HIGH")
        med_count  = sum(1 for c in report.conflicts if c.severity == "MEDIUM")
        tension_note = (
            f" | {len(report.tension_report.tension_pairs)} tension(s) detected."
            if report.tension_report and report.tension_report.has_tension else ""
        )
        if report.conflicts:
            report.summary = (
                f"{len(report.conflicts)} conflict(s) detected "
                f"({high_count} HIGH, {med_count} MEDIUM). "
                f"Risk amplifier: {report.risk_amplifier:.1f}x "
                f"({report.cross_reg_count} regulations cover this domain)."
                + tension_note
                + (" ESCALATION REQUIRED." if high_count else " Review recommended.")
            )
        else:
            report.summary = (
                f"No conflicts detected. "
                f"Domain covered by {report.cross_reg_count} regulation(s). "
                f"Risk amplifier: {report.risk_amplifier:.1f}x."
                + tension_note
            )

        return report

    # ── Check Methods ────────────────────────────────────────────────────────

    def _check_direct_conflicts(
        self, obligation_text: str, domain: str, regulation_ref: str
    ) -> list[ConflictDetail]:
        """Query the ontology for CONFLICTS_WITH edges in this domain."""
        conflict_report = self._pg.find_conflicts(domain, obligation_text)
        details: list[ConflictDetail] = []

        for c in conflict_report.conflicts[:5]:
            node_a = c.get("node_a", {})
            node_b = c.get("node_b", {})
            sev = node_b.get("penalty_severity", "MEDIUM")

            # Determine resolution based on known patterns
            resolution = self._infer_resolution(node_a, node_b, c.get("notes", ""))

            details.append(ConflictDetail(
                conflict_type = "DIRECT",
                severity      = sev,
                regulation_a  = node_a.get("reg_id", "Unknown"),
                regulation_b  = node_b.get("reg_id", "Unknown"),
                description   = c.get("notes", "Regulatory conflict detected."),
                resolution    = resolution,
                precedent_refs= [node_a.get("id", ""), node_b.get("id", "")],
            ))

        return details

    def _check_supersession(
        self, regulation_ref: str, as_of: Optional[date]
    ) -> tuple[list[ConflictDetail], list[dict[str, Any]]]:
        """Check if the cited regulation has been superseded."""
        validity = self._tr.get_obligation_validity(regulation_ref, as_of)
        conflicts: list[ConflictDetail] = []

        if not validity.is_valid:
            # The regulation is no longer active
            superseded_by_refs = [s.get("reg_id", "") for s in validity.superseded_by]
            conflicts.append(ConflictDetail(
                conflict_type = "SUPERSESSION",
                severity      = "HIGH",
                regulation_a  = regulation_ref,
                regulation_b  = ", ".join(superseded_by_refs) or "Unknown successor",
                description   = (
                    f"Regulation '{regulation_ref}' is no longer in force. "
                    + "; ".join(validity.notes)
                ),
                resolution    = (
                    f"Update obligation to reference the current regulation: "
                    + ", ".join(superseded_by_refs)
                ),
                precedent_refs = [s.get("id", "") for s in validity.superseded_by],
            ))
        elif validity.superseded_by:
            # Active but a newer version exists — warn
            for newer in validity.superseded_by:
                conflicts.append(ConflictDetail(
                    conflict_type = "SUPERSESSION",
                    severity      = "MEDIUM",
                    regulation_a  = regulation_ref,
                    regulation_b  = newer.get("reg_id", ""),
                    description   = (
                        f"A newer version exists: {newer.get('reg_id')} "
                        f"(effective {newer.get('effective_date')}). "
                        f"Ensure compliance with the latest requirements."
                    ),
                    resolution    = f"Verify if {newer.get('reg_id')} introduces stricter requirements.",
                    precedent_refs = [newer.get("id", "")],
                ))

        return conflicts, [s for s in validity.superseded_by]

    def _check_standard_downgrade(
        self, compiled: CompiledRule, domain: str
    ) -> list[ConflictDetail]:
        """
        Detect if the obligation specifies an outdated technical standard.

        Example: obligation says AES-128 but current framework mandates AES-256.
        """
        conflicts: list[ConflictDetail] = []

        if not compiled.algorithm:
            return conflicts

        # Known upgrade conflicts
        downgrade_map = {
            ("AES-128", "encryption"): {
                "current":     "AES-256",
                "regulation":  "RBI/2023-24/101",
                "description": "RBI IT Framework 2023 (effective Apr 2024) mandates AES-256 minimum. AES-128 is non-compliant.",
                "resolution":  "Upgrade all encryption to AES-256 with HSM-backed key management.",
            },
            ("TLSv1.2", "cyber"): {
                "current":     "TLSv1.3",
                "regulation":  "RBI/2023-24/101 Annex I",
                "description": "RBI IT Framework 2023 mandates TLS 1.3. TLS 1.2 is deprecated for new implementations.",
                "resolution":  "Upgrade to TLS 1.3 on all internet-facing endpoints.",
            },
            ("RSA-2048", "encryption"): {
                "current":     "RSA-4096",
                "regulation":  "SEBI/HO/ITD/ITD-PoD-1/P/CIR/2023/135",
                "description": "SEBI Cybersecurity Circular 2023 recommends RSA-4096 for new key generation.",
                "resolution":  "Migrate to RSA-4096 or equivalent ECC keys at next rotation cycle.",
            },
        }

        key = (compiled.algorithm, domain)
        if key in downgrade_map:
            info = downgrade_map[key]
            conflicts.append(ConflictDetail(
                conflict_type = "STANDARD_DOWNGRADE",
                severity      = "HIGH",
                regulation_a  = f"Obligation: {compiled.algorithm}",
                regulation_b  = info["regulation"],
                description   = info["description"],
                resolution    = info["resolution"],
            ))

        return conflicts

    def _check_dependency_gaps(self, regulation_ref: str) -> list[str]:
        """Find prerequisite obligations that must be satisfied first."""
        chain = self._pg.get_full_dependency_chain(regulation_ref)
        gaps: list[str] = []
        for dep in chain:
            if not dep.get("mandatory"):
                continue
            # Flag as potential gap — caller must verify actual compliance status
            gaps.append(
                f"{dep.get('reg_id', dep.get('id', 'Unknown'))}: "
                f"{dep.get('summary', 'Prerequisite obligation')} — "
                f"verify this is already satisfied before implementing the current obligation."
            )
        return gaps[:5]

    def _collect_precedents(self, domain: str) -> list[str]:
        """Collect relevant regulatory precedent references for the domain."""
        nodes = self._pg.get_domain_cluster(domain)
        refs = [
            f"{n.get('reg_id', n.get('id', ''))} — {n.get('title', '')[:60]}"
            for n in nodes
            if n.get("mandatory") and n.get("reg_id")
        ]
        return refs[:6]

    @staticmethod
    def _infer_resolution(
        node_a: dict, node_b: dict, notes: str
    ) -> str:
        """Infer the resolution strategy for a conflict."""
        # Known resolution pattern: DPDP erasure vs AML retention
        if "dpdp" in str(node_a).lower() and "aml" in str(node_b).lower():
            return (
                "AML 5-year retention creates lawful basis under DPDP Section 4 (legitimate use). "
                "Implement: retain minimum data fields required by AML; delete all other personal data. "
                "Document the lawful basis in your privacy notice."
            )
        # Encryption standard conflict
        if "aes" in notes.lower() or "encrypt" in notes.lower():
            return (
                "Apply the stricter standard: use AES-256 to satisfy both frameworks. "
                "Decommission AES-128 implementations as per the 2023 framework."
            )
        # Generic resolution
        return (
            "Apply the stricter of the two requirements. "
            "Consult with Legal/Compliance team to document the chosen interpretation."
        )

    # ==========================================================================
    # Phase 9 — Feature 2: Policy Tension Check
    # ==========================================================================

    def _check_policy_tension(
        self,
        obligation_text: str,
        regulation_ref: str,
        domain: str,
    ) -> Optional[PolicyTensionReport]:
        """Delegate to PolicyGraph.detect_policy_tension()."""
        try:
            return self._pg.detect_policy_tension(
                obligation_text, regulation_ref, domain
            )
        except Exception as exc:
            logger.warning("Policy tension check failed: %s", exc)
            return None

    # ==========================================================================
    # Phase 9 — Feature 4: Anti-Hallucination + Evidence Hardening
    # ==========================================================================

    # Weak evidence types that must be challenged
    _WEAK_EVIDENCE: set[str] = {
        "screenshot", "email", "manual_log", "self_attestation",
        "verbal_confirmation", "informal_note", "chat_message",
    }

    # Stronger alternatives for each weak type
    _STRONG_ALTERNATIVES: dict[str, str] = {
        "screenshot":         "API-exported configuration JSON or system-generated audit log",
        "email":              "Signed digital approval in ServiceNow with audit trail hash",
        "manual_log":         "System-generated CSV export with cryptographic checksum",
        "self_attestation":   "Third-party CERT-In audit report or independent assessor finding",
        "verbal_confirmation": "Written Board resolution or signed meeting minutes",
        "informal_note":      "Formal policy document with version control and approver sign-off",
        "chat_message":       "Formal change request ticket with approver chain in ServiceNow",
    }

    def _citation_enforcer(
        self,
        obligation_text: str,
        source_document: str,
        threshold: float = 0.80,
    ) -> Optional[ConflictDetail]:
        """
        Phase 9 — Feature 4: Verify the obligation actually appears in the
        source document using fuzzy matching (difflib.SequenceMatcher).

        If the best fuzzy match ratio < threshold → HALLUCINATION_DETECTED.

        Args:
            obligation_text:  The extracted obligation to verify.
            source_document:  The original regulatory document text.
            threshold:        Minimum similarity ratio to accept (default 0.80).

        Returns ConflictDetail if hallucination is detected, else None.
        """
        if not source_document:
            return None  # Cannot verify without source

        # Slide a window of obligation_text length over source_document
        obl_len = len(obligation_text)
        best_ratio = 0.0
        step = max(1, obl_len // 4)

        src_lower = source_document.lower()
        obl_lower = obligation_text.lower()

        for start in range(0, max(1, len(src_lower) - obl_len + 1), step):
            window = src_lower[start: start + obl_len + 50]
            ratio = difflib.SequenceMatcher(None, obl_lower, window).ratio()
            if ratio > best_ratio:
                best_ratio = ratio
            if best_ratio >= threshold:
                break  # Good enough — early exit

        if best_ratio < threshold:
            logger.warning(
                "HALLUCINATION suspected: obligation not found in source (score=%.3f, threshold=%.2f)",
                best_ratio, threshold
            )
            return ConflictDetail(
                conflict_type = "HALLUCINATION",
                severity      = "HIGH",
                regulation_a  = "LLM Output",
                regulation_b  = "Source Document",
                description   = (
                    f"Obligation text could not be verified against the source document. "
                    f"Fuzzy match score: {best_ratio:.3f} (threshold: {threshold:.2f}). "
                    f"This obligation may have been hallucinated by the LLM."
                ),
                resolution    = (
                    "Re-extract the obligation directly from the source document. "
                    "If no supporting clause is found, discard this obligation."
                ),
            )
        return None

    def _evidence_challenger(
        self,
        evidence_manifest: list[str],
    ) -> list[dict[str, Any]]:
        """
        Phase 9 — Feature 4: Challenge weak evidence types in the manifest.

        Rejects evidence items that are in the _WEAK_EVIDENCE set and
        returns structured rejection flags with stronger alternatives.

        Args:
            evidence_manifest: List of evidence type strings from EvidenceAgent.

        Returns a list of rejection flag dicts (empty if all evidence is strong).
        """
        flags: list[dict[str, Any]] = []
        for evidence_type in evidence_manifest:
            normalized = evidence_type.lower().replace(" ", "_").replace("-", "_")
            if normalized in self._WEAK_EVIDENCE:
                alternative = self._STRONG_ALTERNATIVES.get(normalized, "a stronger, system-generated evidence type")
                flags.append({
                    "status":        "EVIDENCE_REJECTED",
                    "evidence_type": evidence_type,
                    "reason":        f"'{evidence_type}' is classified as weak evidence — easily fabricated or non-auditable.",
                    "alternative":   alternative,
                })
                logger.warning(
                    "Evidence rejected: '%s' — recommend '%s'",
                    evidence_type, alternative
                )
        return flags

    def critique_with_source(
        self,
        obligation_text: str,
        source_document: str,
        evidence_manifest: Optional[list[str]] = None,
        regulation_ref: str = "",
        domain: str = "",
        as_of: Optional[date] = None,
        hallucination_threshold: float = 0.80,
    ) -> CritiqueReport:
        """
        Phase 9 — Feature 4: Full critique with anti-hallucination and
        evidence hardening checks.

        Extends the standard critique() with:
          1. Citation enforcer — verifies obligation against source document
          2. Evidence challenger — rejects weak evidence types

        Args:
            obligation_text:         The obligation to critique.
            source_document:         Original regulatory text for citation check.
            evidence_manifest:       List of evidence types proposed by EvidenceAgent.
            regulation_ref:          Regulatory reference string.
            domain:                  Regulatory domain.
            as_of:                   Date for temporal checks.
            hallucination_threshold: Minimum fuzzy match ratio (default 0.80).

        Returns an extended CritiqueReport with hallucination and evidence flags.
        """
        # Run standard critique first
        report = self.critique(
            obligation_text = obligation_text,
            regulation_ref  = regulation_ref,
            domain          = domain,
            as_of           = as_of,
        )

        # Phase 9 — Feature 4a: Citation enforcer
        hallucination_conflict = self._citation_enforcer(
            obligation_text, source_document, hallucination_threshold
        )
        if hallucination_conflict:
            report.hallucination_detected = True
            report.hallucination_score    = 0.0
            report.conflicts.append(hallucination_conflict)
            report.critique_passed = False
        else:
            report.hallucination_detected = False
            report.hallucination_score    = 1.0

        # Phase 9 — Feature 4b: Evidence challenger
        if evidence_manifest:
            report.evidence_flags = self._evidence_challenger(evidence_manifest)
            if report.evidence_flags:
                # Add a MEDIUM conflict for weak evidence
                report.conflicts.append(ConflictDetail(
                    conflict_type = "WEAK_EVIDENCE",
                    severity      = "MEDIUM",
                    regulation_a  = "Evidence Manifest",
                    regulation_b  = "Audit Requirement",
                    description   = (
                        f"{len(report.evidence_flags)} weak evidence type(s) rejected: "
                        + ", ".join(f['evidence_type'] for f in report.evidence_flags)
                    ),
                    resolution    = (
                        "Replace weak evidence with system-generated, cryptographically "
                        "verifiable alternatives. See evidence_flags for specific recommendations."
                    ),
                ))

        return report
