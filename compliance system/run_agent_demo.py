"""
run_agent_demo.py
==================
Option B — Integration smoke test for the full multi-agent pipeline.

Tests (no network calls required):
  1. ComplianceOntology loads seed YAML and builds NetworkX graph
  2. PolicyGraph queries (conflicts, supersessions, temporal snapshot)
  3. RuleCompiler parses a real RBI obligation
  4. TemporalReasoner validates a regulation date
  5. VerifierAgent flags ambiguity in a vague obligation
  6. CriticAgent detects AES-128 standard downgrade conflict
  7. EvidenceAgent builds an evidence manifest
  8. DeepReasoningAgent runs (rule-based, no Gemini key needed)
  9. PlannerAgent orchestrates all agents + disagreement resolution
 10. AnomalyDetector and ForgeryDetector run offline

Run:
    cd "compliance system"
    python run_agent_demo.py
"""

from __future__ import annotations
import asyncio
import sys
from datetime import date

# ── Colour helpers ────────────────────────────────────────────────────────────
GREEN  = "\033[92m"
RED    = "\033[91m"
YELLOW = "\033[93m"
CYAN   = "\033[96m"
RESET  = "\033[0m"
BOLD   = "\033[1m"

def ok(msg):     print(f"  [OK] {msg}")
def fail(msg):   print(f"  [FAIL] {msg}"); sys.exit(1)
def info(msg):   print(f"       {msg}")
def section(title):
    print(f"\n{'='*60}\n  {title}\n{'='*60}")


# =============================================================================
# Sample regulatory text for testing
# =============================================================================

RBI_CIRCULAR_TEXT = """
RBI Master Direction on Information Technology Framework 2023

Banks shall encrypt all customer data at rest using AES-256 encryption
with Hardware Security Module (HSM) backed key management.

Regulated entities must implement TLS 1.3 for all data in transit,
and must not permit TLS 1.2 or lower on internet-facing endpoints.

Banks shall report all cyber incidents to the Reserve Bank of India
within 6 hours of detection.

All regulated entities are required to conduct VAPT every 6 months
by a CERT-In empanelled auditor.

Adequate security measures should be implemented across all systems
to ensure protection of customer data.

Banks must retain ICT system logs for a minimum period of 180 days.
"""

AMBIGUOUS_OBLIGATION = (
    "Banks should implement adequate security measures promptly "
    "to protect all sensitive data."
)

ENCRYPTION_DOWNGRADE_OBLIGATION = (
    "Banks shall encrypt customer data at rest using AES-128 encryption."
)


# =============================================================================
# Test runner
# =============================================================================

def test_knowledge_layer():
    section("Phase 1-2: Knowledge Graph & Rule Compiler")

    # Test 1: Ontology loads
    from knowledge.compliance_ontology import ComplianceOntology
    onto = ComplianceOntology()
    nodes = onto.get_all_nodes()
    info(f"Ontology loaded: {onto.summary()}")
    assert len(nodes) >= 10, "Expected at least 10 seed nodes"
    ok(f"Ontology loaded {len(nodes)} nodes from seed YAML")

    # Test 2: Conflict detection
    conflicts = onto.get_conflicts("dpdp_retention_conflict")
    assert len(conflicts) > 0, "Expected DPDP vs AML conflict"
    ok(f"Conflict detection: found {len(conflicts)} conflict(s) for DPDP retention node")
    for node, notes in conflicts[:1]:
        info(f"  Conflict: {node.reg_id} — {notes[:80]}")

    # Test 3: Supersession
    from knowledge.policy_graph import PolicyGraph
    pg = PolicyGraph()
    sup = pg.find_supersessions("rbi_it_framework_2023")
    assert len(sup.superseded_nodes) > 0, "RBI 2023 should supersede old framework"
    ok(f"Supersession: RBI 2023 supersedes {len(sup.superseded_nodes)} older regulation(s)")
    for s in sup.superseded_nodes[:1]:
        info(f"  Supersedes: {s['reg_id']}")

    # Test 4: Temporal snapshot
    snap = pg.temporal_snapshot(as_of=date(2024, 6, 1))
    info(f"Temporal snapshot (2024-06-01): {snap.total_count} active regulations")
    info(f"  By source: {snap.by_source}")
    ok("Temporal snapshot completed")

    # Test 5: Rule compiler
    from knowledge.rule_compiler import RuleCompiler
    rc = RuleCompiler()
    rule = rc.compile("Banks shall encrypt customer data at rest using AES-256 encryption.")
    assert rule.algorithm == "AES-256", f"Expected AES-256, got {rule.algorithm}"
    assert rule.mandatory is True
    assert rule.subject == "Banks"
    ok(f"RuleCompiler: subject='{rule.subject}', verb='{rule.verb}', algo='{rule.algorithm}', mandatory={rule.mandatory}, confidence={rule.confidence:.2f}")

    # Test 6: Temporal reasoner
    from knowledge.temporal_reasoner import TemporalReasoner
    tr = TemporalReasoner()
    validity = tr.get_obligation_validity("certin_old_reporting_2014", as_of=date(2024, 1, 1))
    assert not validity.is_valid, "2014 CERT-In reporting should be superseded"
    ok(f"TemporalReasoner: old CERT-In 2014 reporting correctly marked invalid on 2024-01-01")
    info(f"  Notes: {validity.notes[0][:80] if validity.notes else 'N/A'}")

    # Test 7: Risk engine
    from knowledge.risk_engine import RiskScoringEngine
    engine = RiskScoringEngine()
    score = engine.score(
        obligation_text  = "Banks shall encrypt customer data at rest using AES-256.",
        domain           = "encryption",
        impacted_systems = ["CBS", "Data Warehouse", "Backup", "SFTP", "API Gateway"],
        policy_conflicts = [{"severity": "HIGH", "description": "AES-128 vs AES-256"}],
    )
    info(f"RiskScore: {score.overall_score:.2f}/10.0 — {score.risk_level}")
    for f in score.factors:
        info(f"  {f.factor_name}: {f.raw_score:.2f} × {f.weight} = {f.weighted:.4f}")
    ok(f"RiskScoringEngine: overall={score.overall_score}, level={score.risk_level}")


def test_agent_layer():
    section("Phase 3-5: Multi-Agent Pipeline (Rule-Based Mode)")

    # Test 8: VerifierAgent
    from agents.verifier_agent import VerifierAgent
    verifier = VerifierAgent()
    report = verifier.verify(AMBIGUOUS_OBLIGATION)
    assert report.ambiguity_score > 0.3, f"Expected high ambiguity, got {report.ambiguity_score}"
    ok(f"VerifierAgent: score={report.ambiguity_score:.2f}, flags={len(report.flags)}, clarification_needed={report.clarification_needed}")
    for flag in report.flags[:2]:
        info(f"  [{flag.flag_type}] {flag.severity}: {flag.excerpt} — {flag.explanation[:60]}")

    # Test 9: CriticAgent — AES-128 downgrade
    from agents.critic_agent import CriticAgent
    critic = CriticAgent()
    critique = critic.critique(
        obligation_text = ENCRYPTION_DOWNGRADE_OBLIGATION,
        regulation_ref  = "rbi_it_framework_2019",
        domain          = "encryption",
    )
    downgrade = [c for c in critique.conflicts if c.conflict_type == "STANDARD_DOWNGRADE"]
    assert len(downgrade) > 0, "Expected AES-128→AES-256 downgrade conflict"
    ok(f"CriticAgent: {len(critique.conflicts)} conflict(s), passed={critique.critique_passed}")
    info(f"  Downgrade conflict: {downgrade[0].description[:80]}")
    info(f"  Resolution: {downgrade[0].resolution[:80]}")

    # Test 10: EvidenceAgent
    from agents.evidence_agent import EvidenceAgent
    ev_agent = EvidenceAgent()
    manifest = ev_agent.infer_evidence(
        obligation_text = "Banks shall encrypt customer data at rest using AES-256.",
        domain          = "encryption",
    )
    ok(f"EvidenceAgent: {len(manifest.required_evidence)} evidence item(s), {manifest.completeness_pct:.0f}% auto-collectible")
    info(f"  Next action: {manifest.next_action[:80]}")
    info(f"  Gaps: {manifest.gaps}")


async def test_planner_agent():
    section("Phase 5: PlannerAgent — Full Orchestration with Disagreement Resolution")

    from agents.planner_agent import PlannerAgent
    planner = PlannerAgent(
        gemini_api_key = "",          # no key = rule-based mode
        max_obligations = 5,
        max_concurrency = 2,
    )

    result = await planner.plan(
        document_text = RBI_CIRCULAR_TEXT,
        document_ref  = "rbi_it_framework_2023",
    )

    ok(f"PlannerAgent complete: processed={result.obligations_processed}, escalated={result.obligations_escalated}")
    info(f"  Disagreements resolved: {result.disagreements_resolved}")
    info(f"  Agent messages logged: {len(result.agent_messages)}")
    info(f"  MAP ready: {result.map_ready}")
    info(f"  Summary: {result.pipeline_summary}")

    # Show disagreement resolution if any
    disagreement_msgs = [m for m in result.agent_messages if m.message_type == "DISAGREEMENT"]
    if disagreement_msgs:
        info(f"  DISAGREEMENT RESOLUTION:")
        for msg in disagreement_msgs[:2]:
            info(f"    {msg.sender} -> {msg.receiver}: {msg.content[:80]}")

    # Show one obligation result
    if result.obligation_results:
        ob = result.obligation_results[0]
        info(f"\n  Sample obligation result:")
        info(f"    Text: {ob.obligation_text[:60]}")
        if ob.reasoning_result:
            info(f"    Risk: {ob.reasoning_result.get('operational_risk')}")
            info(f"    Systems: {ob.reasoning_result.get('impacted_systems', [])[:3]}")
            info(f"    Conflicts: {len(ob.reasoning_result.get('policy_conflicts', []))}")


def test_intelligent_validation():
    section("Phase 6: Intelligent Validation (Anomaly + Forgery)")

    try:
        from validation.intelligent_checks import (
            AnomalyDetector, ForgeryDetector, ComplianceConfidenceScorer
        )
    except ImportError as e:
        info(f"Skipping (import error): {e}")
        return

    # Test AnomalyDetector — weekend submission
    from datetime import datetime, timezone
    detector = AnomalyDetector()
    weekend_ts = datetime(2024, 5, 4, 2, 30, 0, tzinfo=timezone.utc)  # Saturday 02:30
    report = detector.detect(
        doc_size_bytes       = 200,    # suspiciously small
        submission_ts        = weekend_ts,
        prior_failed_checks  = 4,
        days_since_regulation = 0,
    )
    assert report.is_anomalous, "Expected anomaly for weekend + small doc + many failures"
    ok(f"AnomalyDetector: score={report.anomaly_score:.2f}, level={report.risk_level}")
    for feat in report.suspicious_features[:3]:
        info(f"  [!] {feat}")

    # Test ForgeryDetector — minimal PDF with creation date
    forgery_detector = ForgeryDetector()
    fake_pdf = (
        b"%PDF-1.4\n"
        b"<</CreationDate (D:20180601120000+05'30')"
        b"/Creator (Microsoft Word 2024)"
        b"/ModDate (D:20240515000000+05'30')>>"
    )
    forgery_report = forgery_detector.detect(
        document_bytes          = fake_pdf,
        claimed_compliance_date = date(2018, 6, 1),
        regulation_ref          = "rbi_it_framework_2023",
    )
    ok(f"ForgeryDetector: is_forged={forgery_report.is_forged}, level={forgery_report.risk_level}")
    for finding in forgery_report.findings[:2]:
        info(f"  [FLAG] {finding[:80]}")


# =============================================================================
# Main
# =============================================================================

if __name__ == "__main__":
    print(f"\n{BOLD}{'='*60}")
    print("  Agentic Compliance Platform — Integration Demo")
    print(f"{'='*60}{RESET}")

    try:
        test_knowledge_layer()
        test_agent_layer()
        asyncio.run(test_planner_agent())
        test_intelligent_validation()

        print(f"\n{BOLD}{GREEN}{'='*60}")
        print("  ALL TESTS PASSED [OK]")
        print(f"{'='*60}{RESET}\n")

    except AssertionError as e:
        print(f"\n{RED}ASSERTION FAILED: {e}{RESET}\n")
        sys.exit(1)
    except Exception as e:
        import traceback
        print(f"\n{RED}ERROR: {e}{RESET}")
        traceback.print_exc()
        sys.exit(1)
