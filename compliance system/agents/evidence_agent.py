"""
agents/evidence_agent.py
=========================
EvidenceAgent — automatically infers what evidence is required to prove
compliance with a given obligation, and identifies what can be auto-collected.

For each obligation it produces:
  - required_evidence  : full list of evidence types needed
  - auto_collected     : evidence that can be gathered automatically (ESB/KV queries)
  - manual_required    : evidence that requires human action
  - gaps               : missing evidence that blocks task closure
  - evidence_manifest  : structured checklist for the compliance team
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Optional

from knowledge.rule_compiler import RuleCompiler

logger = logging.getLogger(__name__)


# =============================================================================
# Data Structures
# =============================================================================

@dataclass
class EvidenceItem:
    """A single piece of evidence in the manifest."""
    evidence_id:   str
    evidence_type: str
    description:   str
    collection_method: str   # AUTO | MANUAL | SCREENSHOT | OCR_VERIFY
    source_system: str       # which system provides this evidence
    is_collected:  bool = False
    adls_uri:      str = ""  # ADLS Gen2 path once uploaded
    notes:         str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "evidence_id":       self.evidence_id,
            "evidence_type":     self.evidence_type,
            "description":       self.description,
            "collection_method": self.collection_method,
            "source_system":     self.source_system,
            "is_collected":      self.is_collected,
            "adls_uri":          self.adls_uri,
            "notes":             self.notes,
        }


@dataclass
class EvidenceManifest:
    """Complete evidence requirements for an obligation."""
    obligation_text:  str
    domain:           str
    required_evidence: list[EvidenceItem] = field(default_factory=list)
    auto_collected:   list[EvidenceItem] = field(default_factory=list)
    manual_required:  list[EvidenceItem] = field(default_factory=list)
    gaps:             list[str] = field(default_factory=list)
    completeness_pct: float = 0.0
    next_action:      str = ""

    def to_dict(self) -> dict[str, Any]:
        total = len(self.required_evidence)
        collected = len(self.auto_collected)
        return {
            "obligation_text":  self.obligation_text[:200],
            "domain":           self.domain,
            "total_required":   total,
            "auto_collected":   collected,
            "manual_required":  len(self.manual_required),
            "gaps":             self.gaps,
            "completeness_pct": round(self.completeness_pct, 1),
            "next_action":      self.next_action,
            "evidence_items":   [e.to_dict() for e in self.required_evidence],
        }


# =============================================================================
# Evidence Catalogue (domain-specific IP)
# =============================================================================

# Maps domain → list of (evidence_type, description, collection_method, source_system)
_EVIDENCE_CATALOGUE: dict[str, list[tuple[str, str, str, str]]] = {
    "encryption": [
        ("system_config",           "Encryption algorithm and key size configuration (AES-256)",   "AUTO",       "ESB:encryption_check"),
        ("key_vault_audit_log",     "Azure Key Vault key creation and rotation log",               "AUTO",       "Azure Key Vault"),
        ("tls_scan_report",         "TLS version and cipher suite scan results",                   "AUTO",       "ESB:tls_check"),
        ("encryption_policy_doc",   "IS Policy — Encryption Standards section",                   "MANUAL",     "SharePoint"),
        ("vapt_report",             "VAPT report confirming encryption controls",                   "MANUAL",     "CERT-In Auditor"),
        ("config_screenshot",       "Screenshot of DB/storage encryption settings",                "SCREENSHOT", "DBA/Cloud Admin"),
        ("key_rotation_schedule",   "Key rotation schedule document",                              "AUTO",       "Azure Key Vault"),
    ],
    "cyber": [
        ("soc_screenshot",          "24x7 SOC dashboard screenshot",                               "SCREENSHOT", "SOC Team"),
        ("siem_alert_config",       "SIEM alert rule configuration export",                        "AUTO",       "Microsoft Sentinel"),
        ("vapt_report",             "Bi-annual VAPT report from CERT-In empanelled auditor",       "MANUAL",     "CERT-In Auditor"),
        ("patch_compliance_report", "Patch management compliance report (last 90 days)",           "AUTO",       "ESB:patch_check"),
        ("incident_response_plan",  "Documented Incident Response Plan (IRP)",                     "MANUAL",     "SharePoint"),
        ("network_diagram",         "Network segmentation diagram",                                "MANUAL",     "Network Team"),
        ("access_review_log",       "Quarterly privileged access review log",                      "MANUAL",     "IAM Team"),
    ],
    "kyc": [
        ("kyc_policy_document",     "Board-approved KYC Policy document",                          "MANUAL",     "SharePoint"),
        ("sample_kyc_records",      "Anonymised sample of completed KYC records (10 samples)",    "MANUAL",     "CRM/Operations"),
        ("ckyc_upload_log",         "CKYC registry upload confirmation log",                       "AUTO",       "CKYC Registry API"),
        ("video_kyc_audit_log",     "Video KYC session audit trail",                               "AUTO",       "Video KYC Platform"),
        ("training_certificates",   "Staff training completion records (KYC awareness)",           "MANUAL",     "HR System"),
        ("periodic_update_report",  "Periodic KYC update compliance report",                       "AUTO",       "CRM"),
    ],
    "aml": [
        ("str_filing_log",          "STR filing log for last 12 months (count and dates)",        "AUTO",       "FIU-IND Portal"),
        ("tms_config_screenshot",   "Transaction Monitoring System rule configuration",            "SCREENSHOT", "TMS Admin"),
        ("pep_screening_report",    "PEP and sanctions list screening report",                     "AUTO",       "Watchlist Engine"),
        ("ctr_register",            "Cash Transaction Report (CTR) register",                      "AUTO",       "Core Banking"),
        ("aml_policy_document",     "Board-approved AML/CFT Policy",                              "MANUAL",     "SharePoint"),
        ("staff_training_records",  "AML training completion records",                             "MANUAL",     "HR System"),
    ],
    "data_protection": [
        ("privacy_notice",          "Current Privacy Notice / Privacy Policy document",            "AUTO",       "SharePoint/SP:keyword_scan"),
        ("consent_mechanism_ss",    "Screenshot of consent mechanism on customer portal",          "SCREENSHOT", "Web/App Team"),
        ("data_flow_map",           "Personal Data Flow Mapping document",                         "MANUAL",     "Compliance Team"),
        ("dpia_report",             "Data Protection Impact Assessment (DPIA) report",             "MANUAL",     "DPO"),
        ("erasure_log",             "Data erasure request log and completion timestamps",           "AUTO",       "Data Management System"),
        ("consent_withdrawal_log",  "Consent withdrawal handling log",                             "AUTO",       "CRM"),
        ("dpo_appointment_letter",  "Data Protection Officer appointment letter",                  "MANUAL",     "HR/Legal"),
    ],
    "capital": [
        ("crar_computation",        "CRAR computation sheet (CET1, Tier1, Total CRAR)",           "AUTO",       "Capital Computation Engine"),
        ("rbi_xbrl_return",         "RBI capital adequacy XBRL return submission confirmation",   "AUTO",       "XBRL Reporting Portal"),
        ("icaap_document",          "ICAAP (Internal Capital Adequacy Assessment Process) report", "MANUAL",     "Risk Management"),
        ("board_approval",          "Board Risk Committee approval of capital plan",               "MANUAL",     "Board Secretary"),
    ],
    "liquidity": [
        ("lcr_calculation",         "Daily LCR calculation and HQLA composition",                  "AUTO",       "ALM System"),
        ("rbi_lcr_return",          "Daily LCR return submission to RBI",                          "AUTO",       "Regulatory Reporting"),
        ("stress_test_report",      "30-day liquidity stress test results",                        "MANUAL",     "Treasury/Risk"),
        ("alm_policy",              "Board-approved ALM Policy",                                   "MANUAL",     "SharePoint"),
    ],
    "outsourcing": [
        ("vendor_contracts",        "Signed vendor contracts with security clauses",               "MANUAL",     "Legal/Procurement"),
        ("vendor_assessment_report","Annual vendor security assessment report",                    "MANUAL",     "IT Security"),
        ("data_localisation_proof", "Evidence of India data localisation (storage region configs)", "AUTO",      "Azure Portal / ADLS"),
        ("exit_strategy_doc",       "Documented vendor exit strategy",                             "MANUAL",     "SharePoint"),
        ("cloud_approval_letter",   "RBI approval letter for critical cloud workloads",            "MANUAL",     "RBI Portal"),
    ],
}

# Evidence that can always be auto-collected via existing system integrations
_AUTO_COLLECTIBLE_TYPES = {
    "ESB:encryption_check",
    "ESB:tls_check",
    "ESB:access_control_check",
    "KV:key_policy_check",
    "KV:rotation_schedule_check",
    "SP:version_increment_check",
    "SP:keyword_scan_check",
    "Azure Key Vault",
    "Microsoft Sentinel",
    "Core Banking",
    "CRM",
    "CKYC Registry API",
    "FIU-IND Portal",
    "XBRL Reporting Portal",
    "ALM System",
    "Regulatory Reporting",
    "Capital Computation Engine",
}


# =============================================================================
# Evidence Agent
# =============================================================================

class EvidenceAgent:
    """
    Automatically infers required evidence for a compliance obligation.

    Usage::

        agent = EvidenceAgent()
        manifest = agent.infer_evidence(
            obligation_text="Banks shall encrypt customer data at rest using AES-256.",
            action_items=["Enable AES-256 on CBS", "Configure Key Vault CMK"],
            domain="encryption",
        )
        print(manifest.completeness_pct)
    """

    def __init__(self, rule_compiler: Optional[RuleCompiler] = None):
        self._rc = rule_compiler or RuleCompiler()
        self._evidence_counter = 0

    def infer_evidence(
        self,
        obligation_text: str,
        action_items: Optional[list[str]] = None,
        domain: str = "",
        regulation_ref: str = "",
    ) -> EvidenceManifest:
        """
        Build an evidence manifest for an obligation.

        Steps:
          1. Compile obligation to get domain and rule
          2. Look up evidence catalogue for domain
          3. Check action items for additional evidence hints
          4. Classify each item as AUTO or MANUAL
          5. Identify gaps and compute completeness %
        """
        compiled = self._rc.compile(obligation_text)
        effective_domain = domain or compiled.domain or "cyber"

        manifest = EvidenceManifest(
            obligation_text = obligation_text,
            domain          = effective_domain,
        )

        # Get catalogue items for domain
        catalogue = _EVIDENCE_CATALOGUE.get(effective_domain, _EVIDENCE_CATALOGUE["cyber"])

        # Build evidence items
        evidence_items: list[EvidenceItem] = []
        for ev_type, description, method, source in catalogue:
            item = self._build_item(ev_type, description, method, source)
            evidence_items.append(item)

        # Add evidence from compiled rule's validation_checks
        for check in compiled.validation_checks:
            # Check if already in catalogue
            if not any(check in item.source_system for item in evidence_items):
                item = self._build_item(
                    evidence_type    = check.split(":")[1] if ":" in check else check,
                    description      = f"Automated check: {check}",
                    collection_method = "AUTO",
                    source_system    = check,
                )
                evidence_items.append(item)

        # Enrich with action item hints
        if action_items:
            for action in action_items:
                extra = self._evidence_from_action(action, effective_domain)
                for ev in extra:
                    if not any(ev.evidence_type == e.evidence_type for e in evidence_items):
                        evidence_items.append(ev)

        manifest.required_evidence = evidence_items
        manifest.auto_collected    = [e for e in evidence_items if e.collection_method == "AUTO"]
        manifest.manual_required   = [e for e in evidence_items if e.collection_method != "AUTO"]

        # Identify gaps
        manifest.gaps = self._identify_gaps(evidence_items, effective_domain)

        # Completeness = auto_collected / total
        total = len(evidence_items)
        if total > 0:
            manifest.completeness_pct = (len(manifest.auto_collected) / total) * 100

        # Next action
        if manifest.gaps:
            manifest.next_action = (
                f"BLOCKED: {len(manifest.gaps)} critical evidence gap(s). "
                f"Manual collection required: {manifest.gaps[0]}"
            )
        elif manifest.manual_required:
            manifest.next_action = (
                f"AUTO-COLLECTION READY. "
                f"{len(manifest.manual_required)} manual item(s) pending. "
                f"Assign to: {manifest.manual_required[0].source_system}"
            )
        else:
            manifest.next_action = "ALL EVIDENCE AUTO-COLLECTIBLE. Trigger ESB validation pipeline."

        return manifest

    def infer_evidence_batch(
        self,
        obligations: list[dict[str, Any]],
    ) -> list[EvidenceManifest]:
        """
        Process multiple obligations.
        Each dict: {"text": str, "domain": str, "action_items": list[str]}
        """
        return [
            self.infer_evidence(
                obligation_text = ob.get("text", ""),
                action_items    = ob.get("action_items"),
                domain          = ob.get("domain", ""),
                regulation_ref  = ob.get("regulation_ref", ""),
            )
            for ob in obligations
        ]

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _build_item(
        self,
        evidence_type: str,
        description: str,
        collection_method: str,
        source_system: str,
    ) -> EvidenceItem:
        self._evidence_counter += 1
        is_auto = (
            collection_method == "AUTO"
            or any(auto in source_system for auto in _AUTO_COLLECTIBLE_TYPES)
        )
        return EvidenceItem(
            evidence_id        = f"EV-{self._evidence_counter:04d}",
            evidence_type      = evidence_type,
            description        = description,
            collection_method  = "AUTO" if is_auto else collection_method,
            source_system      = source_system,
            is_collected       = is_auto,  # assume auto items will be collected
        )

    def _evidence_from_action(
        self, action_text: str, domain: str
    ) -> list[EvidenceItem]:
        """Extract additional evidence hints from an action item description."""
        items: list[EvidenceItem] = []
        action_lower = action_text.lower()

        if "screenshot" in action_lower or "capture" in action_lower:
            items.append(self._build_item(
                "action_screenshot",
                f"Screenshot evidence for: {action_text[:80]}",
                "SCREENSHOT",
                "Cloud Admin / System Owner",
            ))
        if "policy" in action_lower or "document" in action_lower:
            items.append(self._build_item(
                "policy_document",
                f"Policy/document evidence for: {action_text[:80]}",
                "MANUAL",
                "SharePoint",
            ))
        if "test" in action_lower or "vapt" in action_lower:
            items.append(self._build_item(
                "test_report",
                f"Test/audit report for: {action_text[:80]}",
                "MANUAL",
                "CERT-In Auditor / QA Team",
            ))
        return items

    def _identify_gaps(
        self, evidence_items: list[EvidenceItem], domain: str
    ) -> list[str]:
        """
        Identify critical evidence gaps that would block task closure.
        A gap is a MANUAL item with no ADLS URI and no auto-collection possible.
        """
        gaps: list[str] = []
        # Policy document is always required
        has_policy = any("policy" in e.evidence_type.lower() for e in evidence_items)
        if not has_policy:
            gaps.append(f"Missing: Board-approved {domain.replace('_', ' ').title()} Policy document")

        # VAPT/audit report for cyber-related domains
        if domain in ("encryption", "cyber"):
            has_vapt = any("vapt" in e.evidence_type.lower() for e in evidence_items)
            if not has_vapt:
                gaps.append("Missing: VAPT report from CERT-In empanelled auditor")

        # Screenshot evidence for technical configs
        has_screenshot = any(e.collection_method == "SCREENSHOT" for e in evidence_items)
        if domain in ("encryption", "cyber", "aml") and not has_screenshot:
            gaps.append(f"Missing: System configuration screenshot for {domain} controls")

        return gaps[:3]  # top 3 gaps
