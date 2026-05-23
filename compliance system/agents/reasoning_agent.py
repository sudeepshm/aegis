"""
agents/reasoning_agent.py
==========================
DeepReasoningAgent — Gemini-powered chain-of-thought reasoning on obligations.

For each obligation, produces:
  - impacted_systems       : which systems are affected
  - inferred_cloud_deps    : cloud service dependencies
  - policy_conflicts       : conflicts with existing policies (from ontology)
  - operational_risk       : risk level with justification
  - rollout_sequence       : ordered implementation steps
  - evidence_auto_identified: evidence types automatically identified
  - confidence             : overall reasoning confidence

Falls back to ontology-only reasoning if Gemini is unavailable.
"""
from __future__ import annotations

import json
import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from knowledge.policy_graph import PolicyGraph
from knowledge.rule_compiler import RuleCompiler, CompiledRule
from knowledge.temporal_reasoner import TemporalReasoner
from knowledge.dag_validator import DAGValidator

logger = logging.getLogger(__name__)

# ── Optional Gemini import ─────────────────────────────────────────────────────
try:
    from langchain_google_genai import ChatGoogleGenerativeAI
    _GEMINI_AVAILABLE = True
except ImportError:
    _GEMINI_AVAILABLE = False
    logger.warning("langchain-google-genai not installed — DeepReasoningAgent will use rule-based fallback")


# =============================================================================
# Data Structures
# =============================================================================

@dataclass
class ReasoningResult:
    """Full output of DeepReasoningAgent for a single obligation."""
    reasoning_id:            str = field(default_factory=lambda: str(uuid.uuid4()))
    obligation_text:         str = ""
    compiled_rule:           Optional[dict[str, Any]] = None
    impacted_systems:        list[str] = field(default_factory=list)
    inferred_cloud_deps:     list[str] = field(default_factory=list)
    policy_conflicts:        list[dict[str, Any]] = field(default_factory=list)
    operational_risk:        str = "MEDIUM"
    risk_justification:      str = ""
    # Phase 9: DAG format — list[dict] with 'task' and 'depends_on' keys
    rollout_sequence:        list[dict] = field(default_factory=list)
    dag_nodes:               list[dict] = field(default_factory=list)   # validated, topo-sorted
    dag_validation:          Optional[dict[str, Any]] = None
    evidence_auto_identified: list[str] = field(default_factory=list)
    supersession_notes:      list[str] = field(default_factory=list)
    confidence:              float = 0.0
    reasoning_method:        str = "rule_based"   # "gemini" | "rule_based" | "hybrid"
    generated_at:            str = field(
        default_factory=lambda: datetime.now(tz=timezone.utc).isoformat()
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "reasoning_id":            self.reasoning_id,
            "obligation_text":         self.obligation_text[:200],
            "compiled_rule":           self.compiled_rule,
            "impacted_systems":        self.impacted_systems,
            "inferred_cloud_deps":     self.inferred_cloud_deps,
            "policy_conflicts":        self.policy_conflicts,
            "operational_risk":        self.operational_risk,
            "risk_justification":      self.risk_justification,
            "rollout_sequence":        self.rollout_sequence,
            "dag_nodes":               self.dag_nodes,
            "dag_validation":          self.dag_validation,
            "evidence_auto_identified": self.evidence_auto_identified,
            "supersession_notes":      self.supersession_notes,
            "confidence":              round(self.confidence, 4),
            "reasoning_method":        self.reasoning_method,
            "generated_at":            self.generated_at,
        }


# =============================================================================
# System & Cloud Knowledge Maps (domain-specific IP)
# =============================================================================

# Maps compliance domain → likely impacted systems in a bank/NBFC
_DOMAIN_TO_SYSTEMS: dict[str, list[str]] = {
    "encryption": [
        "Core Banking System (CBS)", "Data Warehouse", "Backup & DR Systems",
        "API Gateway", "Mobile Banking App Backend", "SFTP File Transfer",
    ],
    "cyber": [
        "Firewall / WAF", "SIEM Platform", "Endpoint Detection (EDR)",
        "Privileged Access Management (PAM)", "Network IDS/IPS",
    ],
    "kyc": [
        "Customer Onboarding System", "Video KYC Platform", "CRM",
        "CKYC Registry Integration", "Aadhaar eKYC API",
    ],
    "aml": [
        "Transaction Monitoring System (TMS)", "FIU-IND Reporting Portal",
        "Watchlist Screening Engine", "Case Management System",
    ],
    "data_protection": [
        "Customer Database", "Data Lake / ADLS Gen2", "CRM",
        "Marketing Automation Platform", "Analytics Platform",
    ],
    "capital": [
        "Capital Computation Engine", "RBI Regulatory Reporting (XBRL)",
        "Risk Data Aggregation System", "General Ledger",
    ],
    "liquidity": [
        "Asset Liability Management (ALM) System", "Treasury Management System",
        "Real-Time Gross Settlement (RTGS) Integration",
    ],
    "outsourcing": [
        "Vendor Management System", "Contract Repository",
        "Third-Party Risk Assessment Platform",
    ],
    "fraud": [
        "Fraud Detection Engine (ML)", "Transaction Monitoring System",
        "Card Management System", "Alert & Case Management",
    ],
    "audit": [
        "Internal Audit Management System", "Document Management (SharePoint)",
        "GRC Platform", "Regulatory Reporting Portal",
    ],
}

# Maps algorithm → Azure cloud service dependencies
_ALGO_TO_CLOUD_DEPS: dict[str, list[str]] = {
    "AES-256":  ["Azure Storage with Customer-Managed Keys (CMK)", "Azure Key Vault (HSM-backed)"],
    "AES-128":  ["Azure Storage Encryption (platform-managed)"],
    "TLSv1.3":  ["Azure Application Gateway with TLS 1.3 policy", "Azure Front Door"],
    "TLSv1.2":  ["Azure Application Gateway (TLS 1.2)"],
    "RSA-4096": ["Azure Key Vault (RSA-4096 keys)"],
    "MFA":      ["Azure Active Directory MFA", "Microsoft Authenticator"],
    "SIEM":     ["Microsoft Sentinel (Azure SIEM)", "Azure Monitor / Log Analytics"],
    "VAPT":     ["Azure Defender for Cloud", "CERT-In Empanelled Auditor"],
    "ISO-27001":["Azure Policy (ISO 27001 blueprint)", "Azure Security Center"],
}

# Rollout sequence templates per domain
_ROLLOUT_TEMPLATES: dict[str, list[str]] = {
    "encryption": [
        "1. Inventory all systems storing target data",
        "2. Assess current encryption configuration on each system",
        "3. Procure / enable Azure Key Vault HSM for key management",
        "4. Enable Customer-Managed Keys (CMK) on ADLS Gen2 / Blob Storage",
        "5. Migrate Core Banking DB to AES-256 (coordinate downtime window)",
        "6. Update Backup & DR encryption policy",
        "7. Rotate old encryption keys and document in Key Vault",
        "8. Run VAPT to validate encryption implementation",
        "9. Collect system configuration screenshots as evidence",
        "10. File compliance certificate with Internal Audit",
    ],
    "cyber": [
        "1. Gap assessment against regulation requirements",
        "2. Update IS Policy document",
        "3. Deploy / upgrade SIEM with required log sources",
        "4. Configure alerting rules per regulation thresholds",
        "5. Conduct VAPT by CERT-In empanelled auditor",
        "6. Implement missing controls (MFA, RBAC, network segmentation)",
        "7. Conduct tabletop incident response exercise",
        "8. Document evidence and file with Internal Audit",
    ],
    "kyc": [
        "1. Review current CDD / EDD procedures against new requirements",
        "2. Update KYC policy document and customer onboarding workflows",
        "3. Integrate with CKYC registry for existing customer records",
        "4. Enable periodic KYC update reminders (2/8/10 year cycles)",
        "5. Train operations staff on new KYC standards",
        "6. Submit updated KYC policy to RBI if required",
    ],
    "aml": [
        "1. Review STR / CTR threshold and reporting timelines",
        "2. Update transaction monitoring rules in TMS",
        "3. Configure FIU-IND reporting integration",
        "4. Update PEP / sanctions screening lists",
        "5. Train compliance team on updated AML obligations",
        "6. File revised AML policy with Board Risk Committee",
    ],
    "data_protection": [
        "1. Map all personal data flows (Data Flow Mapping)",
        "2. Update Privacy Notice and Consent Mechanism",
        "3. Implement data minimisation controls in onboarding",
        "4. Configure data retention and auto-deletion policies",
        "5. Establish Data Principal Rights portal (access, erasure, correction)",
        "6. Conduct DPIA for high-risk processing activities",
        "7. Appoint Data Protection Officer (if Significant Data Fiduciary)",
        "8. Register with Data Protection Board when operational",
    ],
    "capital": [
        "1. Validate capital computation engine against RBI Master Circular",
        "2. Reconcile CET1 / Tier 1 / CRAR ratios",
        "3. Ensure Capital Conservation Buffer (CCB) of 2.5% is maintained",
        "4. Prepare ICAAP document for Board approval",
        "5. Submit capital adequacy returns to RBI via XBRL",
    ],
    "liquidity": [
        "1. Review LCR calculation against amended run-off rates",
        "2. Update ALM system with new internet deposit run-off (15%)",
        "3. Validate HQLA composition and concentration limits",
        "4. Configure daily LCR reporting to RBI",
        "5. Stress test liquidity position under amended scenario",
    ],
}


# =============================================================================
# Deep Reasoning Agent
# =============================================================================

class DeepReasoningAgent:
    """
    Chain-of-thought reasoning agent for compliance obligations.

    Uses Gemini API for language-heavy reasoning steps (conflict summarisation,
    risk narrative) and the compliance ontology for deterministic steps
    (system identification, supersession detection, evidence inference).

    This hybrid approach ensures deterministic, auditable output even when
    the LLM is unavailable or returns low-confidence results.
    """

    # Chain-of-thought prompt template for Gemini
    _COT_PROMPT = """You are a senior compliance architect specialising in Indian banking regulation.

Analyse this regulatory obligation and respond ONLY with a valid JSON object.

OBLIGATION:
{obligation_text}

COMPILED RULE (pre-extracted):
{compiled_rule_json}

KNOWN POLICY CONFLICTS (from ontology):
{conflicts_json}

REGULATION SUPERSESSION INFO:
{supersession_json}

IMPORTANT — rollout_sequence must be a Directed Acyclic Graph (DAG), NOT a flat list.
For every action item, identify its technical prerequisites.
Output rollout_sequence as a JSON array of objects:
  [{{"task": "Enable MFA", "depends_on": ["Upgrade IAM to v2", "Configure Azure AD"]}},
   {{"task": "Upgrade IAM to v2", "depends_on": []}},
   {{"task": "Configure Azure AD", "depends_on": []}}]
Rules:
- Each task name must be unique.
- depends_on must only reference other task names in the same array.
- There must be NO circular dependencies.
- Tasks with empty depends_on are root tasks (start immediately).

Respond ONLY with this JSON structure (no markdown, no explanation outside JSON):
{{
  "impacted_systems": ["list of specific IT systems impacted"],
  "inferred_cloud_deps": ["list of specific Azure/cloud services needed"],
  "policy_conflicts": ["list of specific conflict descriptions"],
  "operational_risk": "HIGH | MEDIUM | LOW",
  "risk_justification": "2-3 sentence explanation of risk assessment",
  "rollout_sequence": [{{"task": "...", "depends_on": []}}],
  "evidence_auto_identified": ["list of evidence types needed"],
  "supersession_notes": ["any supersession flags relevant to this obligation"],
  "confidence": 0.0
}}"""

    def __init__(
        self,
        gemini_api_key: str = "",
        gemini_model: str = "gemini-1.5-pro",
        policy_graph: Optional[PolicyGraph] = None,
        rule_compiler: Optional[RuleCompiler] = None,
        temporal_reasoner: Optional[TemporalReasoner] = None,
    ):
        self._pg = policy_graph or PolicyGraph()
        self._rc = rule_compiler or RuleCompiler()
        self._tr = temporal_reasoner or TemporalReasoner()
        self._llm = None

        if _GEMINI_AVAILABLE and gemini_api_key:
            try:
                self._llm = ChatGoogleGenerativeAI(
                    model=gemini_model,
                    google_api_key=gemini_api_key,
                    temperature=0.1,   # low temperature for factual compliance output
                )
                logger.info("DeepReasoningAgent: Gemini LLM initialised (%s)", gemini_model)
            except Exception as exc:
                logger.warning("Gemini init failed: %s — using rule-based fallback", exc)

    async def reason(
        self,
        obligation_text: str,
        regulation_ref: str = "",
        domain: str = "",
    ) -> ReasoningResult:
        """
        Run 6-step chain-of-thought reasoning on an obligation.

        Steps:
          1. Compile obligation text → CompiledRule (deterministic)
          2. Identify impacted systems (ontology-based)
          3. Infer cloud dependencies (rule-based mapping)
          4. Detect policy conflicts (ontology query)
          5. Check supersession (temporal reasoner)
          6. Score operational risk + generate rollout sequence
        """
        result = ReasoningResult(obligation_text=obligation_text)

        # Step 1: Compile to structured rule
        compiled = self._rc.compile(obligation_text)
        result.compiled_rule = compiled.to_dict()

        # Use compiled rule's domain if not provided
        effective_domain = domain or compiled.domain or "cyber"

        # Step 2: Impacted systems (deterministic)
        result.impacted_systems = self._identify_systems(effective_domain, compiled)

        # Step 3: Cloud dependencies (deterministic)
        result.inferred_cloud_deps = self._infer_cloud_deps(compiled)

        # Step 4: Policy conflicts (ontology query)
        conflict_report = self._pg.find_conflicts(effective_domain, obligation_text)
        result.policy_conflicts = [
            {
                "description": c.get("notes", ""),
                "node_a": c.get("node_a", {}).get("reg_id", ""),
                "node_b": c.get("node_b", {}).get("reg_id", ""),
                "severity": c.get("node_b", {}).get("penalty_severity", "MEDIUM"),
            }
            for c in conflict_report.conflicts[:5]  # top 5
        ]

        # Step 5: Supersession check
        if regulation_ref:
            supersession = self._pg.find_supersessions(regulation_ref)
            result.supersession_notes = supersession.notes

        # Step 6: Evidence identification (deterministic)
        result.evidence_auto_identified = self._identify_evidence(compiled, effective_domain)

        # Step 7: Risk scoring (deterministic baseline)
        result.operational_risk, result.risk_justification = self._score_risk(
            compiled, result.policy_conflicts, result.impacted_systems
        )

        # Step 8: Rollout sequence — build DAG from domain template
        template_steps = _ROLLOUT_TEMPLATES.get(
            effective_domain, _ROLLOUT_TEMPLATES.get("cyber", [])
        )
        result.rollout_sequence = self._template_to_dag(template_steps)

        # If Gemini available, enrich with LLM reasoning (override where LLM adds value)
        if self._llm:
            result = await self._enrich_with_gemini(result, compiled, conflict_report)
            result.reasoning_method = "hybrid"
        else:
            result.reasoning_method = "rule_based"
            result.confidence = compiled.confidence * 0.85

        # Step 9 (Phase 9): Validate and store the DAG
        result = self._build_dag(result)

        return result

    # ── DAG Construction ─────────────────────────────────────────────────────

    @staticmethod
    def _template_to_dag(template_steps: list[str]) -> list[dict]:
        """
        Convert flat ordered template steps into a linear DAG:
          step[i] depends_on step[i-1].

        This creates a simple sequential chain which Gemini enrichment
        can then elaborate into a richer branching DAG.
        """
        if not template_steps:
            return []
        dag: list[dict] = []
        for i, step in enumerate(template_steps):
            # Strip leading numbering ("1. Do X" -> "Do X")
            task_name = step.lstrip("0123456789. ").strip()
            depends_on = [dag[i - 1]["task"]] if i > 0 else []
            dag.append({"task": task_name, "depends_on": depends_on})
        return dag

    def _build_dag(self, result: "ReasoningResult") -> "ReasoningResult":
        """
        Phase 9: Validate the rollout_sequence DAG and populate dag_nodes
        with the topologically ordered execution plan.

        If the DAG has cycles (LLM hallucination), falls back to the
        deterministic sequential template.
        """
        validator = DAGValidator()
        validation = validator.validate(result.rollout_sequence)

        if validation.is_valid:
            result.dag_nodes = [
                next(n for n in result.rollout_sequence if n["task"] == t)
                for t in validation.topological_order
                if any(n["task"] == t for n in result.rollout_sequence)
            ]
            result.dag_validation = validation.to_dict()
        else:
            logger.warning(
                "DAG validation failed (%s) — using sequential fallback",
                validation.errors
            )
            # Rebuild as safe sequential chain
            result.rollout_sequence = self._template_to_dag(
                [n.get("task", str(n)) for n in result.rollout_sequence]
            )
            validation2 = validator.validate(result.rollout_sequence)
            result.dag_nodes = result.rollout_sequence
            result.dag_validation = validation2.to_dict()

        return result

    # ── Deterministic Steps ──────────────────────────────────────────────────

    def _identify_systems(self, domain: str, rule: CompiledRule) -> list[str]:
        systems = list(_DOMAIN_TO_SYSTEMS.get(domain, []))
        # Add algorithm-specific system hints
        if rule.algorithm in ("AES-256", "AES-128") and "Core Banking" not in str(systems):
            systems.insert(0, "Core Banking System (CBS)")
        if "backup" in rule.object.lower():
            systems.append("Backup & Disaster Recovery Systems")
        if "cloud" in rule.qualifier.lower():
            systems.append("Cloud-Hosted Applications (ADLS Gen2, Azure Blob)")
        return list(dict.fromkeys(systems))[:8]  # deduplicate, cap at 8

    def _infer_cloud_deps(self, rule: CompiledRule) -> list[str]:
        deps: list[str] = []
        if rule.algorithm and rule.algorithm in _ALGO_TO_CLOUD_DEPS:
            deps.extend(_ALGO_TO_CLOUD_DEPS[rule.algorithm])
        for check in rule.validation_checks:
            if "KV:" in check:
                deps.append("Azure Key Vault (for secret/key management)")
                break
        if rule.evidence_type == "system_config":
            deps.append("Azure Policy (compliance scanning)")
        return list(dict.fromkeys(deps))[:6]

    def _identify_evidence(self, rule: CompiledRule, domain: str) -> list[str]:
        evidence: list[str] = [rule.evidence_type]
        # Add domain-specific evidence
        if domain == "encryption":
            evidence += ["encryption_config_screenshot", "key_vault_audit_log", "penetration_test_report"]
        elif domain == "cyber":
            evidence += ["vapt_report", "soc_dashboard_screenshot", "incident_response_plan"]
        elif domain == "kyc":
            evidence += ["kyc_policy_document", "sample_kyc_records", "training_certificates"]
        elif domain == "aml":
            evidence += ["str_filing_log", "transaction_monitoring_config", "pep_screening_report"]
        elif domain == "data_protection":
            evidence += ["privacy_notice", "consent_mechanism_screenshot", "dpia_report"]
        return list(dict.fromkeys(evidence))

    def _score_risk(
        self,
        rule: CompiledRule,
        conflicts: list[dict[str, Any]],
        systems: list[str],
    ) -> tuple[str, str]:
        score = 0
        if rule.mandatory:
            score += 3
        if conflicts:
            score += len(conflicts) * 2
        if len(systems) >= 5:
            score += 2
        if rule.deadline_days <= 14:
            score += 2
        if rule.algorithm in ("AES-256", "TLSv1.3"):
            score += 1  # technical complexity

        if score >= 7:
            risk = "HIGH"
            justification = (
                f"HIGH risk: mandatory obligation affecting {len(systems)} systems "
                f"with {len(conflicts)} policy conflict(s) and {rule.deadline_days}-day deadline. "
                f"Requires immediate escalation to CISO and Compliance Officer."
            )
        elif score >= 4:
            risk = "MEDIUM"
            justification = (
                f"MEDIUM risk: affects {len(systems)} systems "
                f"with {rule.deadline_days}-day implementation window. "
                f"Assign to IT Security team with monthly progress review."
            )
        else:
            risk = "LOW"
            justification = (
                f"LOW risk: limited system impact ({len(systems)} systems), "
                f"no major policy conflicts, {rule.deadline_days}-day deadline is achievable."
            )
        return risk, justification

    # ── Gemini Enrichment ────────────────────────────────────────────────────

    async def _enrich_with_gemini(
        self,
        result: ReasoningResult,
        compiled: CompiledRule,
        conflict_report: Any,
    ) -> ReasoningResult:
        """Call Gemini with chain-of-thought prompt, merge results."""
        try:
            prompt = self._COT_PROMPT.format(
                obligation_text    = result.obligation_text[:2000],
                compiled_rule_json = json.dumps(compiled.to_dict(), indent=2)[:500],
                conflicts_json     = json.dumps(result.policy_conflicts[:3], indent=2)[:400],
                supersession_json  = json.dumps(result.supersession_notes[:3], indent=2)[:300],
            )
            response = await self._llm.ainvoke(prompt)
            content  = response.content if hasattr(response, "content") else str(response)
            parsed   = self._parse_gemini_response(content)

            if parsed:
                # Override only fields where Gemini adds clear value
                if parsed.get("impacted_systems"):
                    result.impacted_systems = parsed["impacted_systems"][:8]
                if parsed.get("inferred_cloud_deps"):
                    result.inferred_cloud_deps = parsed["inferred_cloud_deps"][:6]
                if parsed.get("risk_justification"):
                    result.risk_justification = parsed["risk_justification"]
                if parsed.get("operational_risk") in ("HIGH", "MEDIUM", "LOW"):
                    result.operational_risk = parsed["operational_risk"]
                # Phase 9: accept list[dict] DAG format from Gemini
                raw_seq = parsed.get("rollout_sequence", [])
                if raw_seq:
                    # Gemini may return list[str] (old format) — coerce to DAG
                    if raw_seq and isinstance(raw_seq[0], str):
                        result.rollout_sequence = self._template_to_dag(raw_seq[:12])
                    else:
                        result.rollout_sequence = [r for r in raw_seq[:12]
                                                   if isinstance(r, dict) and "task" in r]
                result.confidence = float(parsed.get("confidence", 0.85))
            else:
                result.confidence = compiled.confidence * 0.85
                logger.warning("Gemini response unparseable — keeping rule-based results")

        except Exception as exc:
            logger.error("Gemini enrichment failed: %s — keeping rule-based results", exc)
            result.confidence = compiled.confidence * 0.85

        return result

    @staticmethod
    def _parse_gemini_response(content: str) -> Optional[dict]:
        """Extract JSON from Gemini response, handling markdown fences."""
        # Strip markdown fences
        cleaned = re.sub(r"```(?:json)?\s*", "", content, flags=re.I).strip().rstrip("`").strip()
        # Try direct parse
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            pass
        # Find first JSON object
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start != -1 and end > start:
            try:
                return json.loads(cleaned[start:end + 1])
            except json.JSONDecodeError:
                pass
        return None
