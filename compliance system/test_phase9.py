"""
Phase 9 Enterprise Intelligence Upgrade — Unit Test Suite
Tests all 5 features end-to-end.
"""
import asyncio
import sys
from datetime import date


def section(title):
    print(f"\n{'=' * 60}")
    print(f"  {title}")
    print('=' * 60)


# ── Feature 1: DAG Validator ──────────────────────────────────────────────────
section("Feature 1: DAG Validator")
from knowledge.dag_validator import DAGValidator
v = DAGValidator()

dag = [
    {"task": "Enable MFA",        "depends_on": ["Upgrade IAM", "Configure Azure AD"]},
    {"task": "Upgrade IAM",       "depends_on": []},
    {"task": "Configure Azure AD","depends_on": []},
    {"task": "Run VAPT",          "depends_on": ["Enable MFA"]},
]
result = v.validate(dag)
assert result.is_valid, f"DAG invalid: {result.errors}"
assert "Enable MFA" in result.topological_order
assert result.critical_path
print(f"  [OK] Valid DAG | topo_order: {result.topological_order}")
print(f"  [OK] Critical path: {result.critical_path}")

# Cycle detection
cycle_dag = [
    {"task": "A", "depends_on": ["B"]},
    {"task": "B", "depends_on": ["C"]},
    {"task": "C", "depends_on": ["A"]},
]
cr = v.validate(cycle_dag)
assert cr.has_cycles, "Should detect cycle"
print(f"  [OK] Cycle detected: {cr.cycles}")

# ServiceNow hierarchy
snow = v.build_servicenow_hierarchy(dag)
roots    = [n for n in snow if n["snow_state"] == "New"]
children = [n for n in snow if n["snow_state"] == "On Hold"]
assert len(roots) == 2
assert len(children) == 2
print(f"  [OK] ServiceNow: {len(roots)} root tasks (New), {len(children)} children (On Hold)")


# ── Feature 2: Policy Tension Detection ──────────────────────────────────────
section("Feature 2: Policy Tension Detection")
from knowledge.policy_graph import PolicyGraph
pg = PolicyGraph()
tension = pg.detect_policy_tension(
    "Banks must retain all customer transaction records for 5 years.",
    "rbi_aml", "aml"
)
print(f"  [OK] Tension detected: {tension.has_tension}")
print(f"  [OK] Summary: {tension.summary[:100]}")
if tension.compensating_controls:
    print(f"  [OK] {len(tension.compensating_controls)} compensating control(s) found")
    print(f"  [OK] First: {tension.compensating_controls[0][:80]}...")


# ── Feature 3: Blast Radius Simulator ────────────────────────────────────────
section("Feature 3: Blast Radius Simulator")
from knowledge.simulator import BlastRadiusSimulator
sim = BlastRadiusSimulator()

report = sim.simulate_delay(
    dag=dag,
    delayed_task="Upgrade IAM",
    delay_days=30,
    regulation_deadlines={
        "Enable MFA": date(2024, 7, 1),
        "Run VAPT":   date(2024, 8, 1),
    },
)
assert report.total_tasks_impacted == 2, f"Expected 2, got {report.total_tasks_impacted}"
assert report.critical_path_broken
assert report.risk_delta_pct > 0
print(f"  [OK] Impacted tasks: {report.total_tasks_impacted}")
print(f"  [OK] Critical path broken: {report.critical_path_broken}")
print(f"  [OK] Risk delta: +{report.risk_delta_pct:.1f}%")
print(f"  [OK] Narrative: {report.narrative[:120]}")
print(f"  [OK] Deadlines breached: {len(report.breached_deadlines)}")
if report.breached_deadlines:
    b = report.breached_deadlines[0]
    print(f"       {b.task_name}: {b.breach_days}d late, tier={b.penalty_tier}")


# ── Feature 4: Anti-Hallucination + Evidence Hardening ───────────────────────
section("Feature 4: Anti-Hallucination + Evidence Hardening")
from agents.critic_agent import CriticAgent
critic = CriticAgent()

source_doc = "Banks shall encrypt all customer data using AES-256 at rest and in transit."

# Citation pass (real obligation)
r_pass = critic.critique_with_source(
    "Banks shall encrypt all customer data using AES-256",
    source_doc,
)
assert not r_pass.hallucination_detected
print(f"  [OK] Citation PASS | score={r_pass.hallucination_score:.4f}")

# Citation fail (hallucinated obligation)
r_fake = critic.critique_with_source(
    "Banks must publish quarterly alien invasion contingency plans.",
    source_doc,
    hallucination_threshold=0.80,
)
assert r_fake.hallucination_detected
print(f"  [OK] Citation FAIL (hallucination) | score={r_fake.hallucination_score:.4f}")

# Tension report in CritiqueReport
r_tension = critic.critique(
    "Banks must retain all customer transaction records for 5 years.",
    regulation_ref="rbi_aml",
    domain="aml",
)
print(f"  [OK] Tension in critique: has_tension={r_tension.tension_report.has_tension if r_tension.tension_report else 'N/A'}")

# Evidence challenger
weak_evidence = ["screenshot", "email", "self_attestation"]
flags = critic._evidence_challenger(weak_evidence)
assert len(flags) == 3
for f in flags:
    assert f["status"] == "EVIDENCE_REJECTED"
    ev_type = f["evidence_type"]
    alt = f["alternative"]
    print(f"  [OK] Rejected: '{ev_type}' -> '{alt[:55]}...'")


# ── Feature 5: Correction Ledger + Router ────────────────────────────────────
section("Feature 5: Correction Ledger + Router Few-Shot")
from audit.logger import AuditLogger, AuditLoggerConfig, CorrectionLedgerClient
from routing.router import HybridRouter


async def test_feature_5():
    audit = AuditLogger(AuditLoggerConfig())

    # Ensure we have a chain to log against
    # log_human_correction needs a chain — use new_chain then append
    chain_id = audit.new_chain()

    correction_id = await audit.log_human_correction(
        chain_id        = chain_id,
        corrected_by    = "cco@bank.org",
        stage           = "routing",
        original_value  = {"department": "IT Security"},
        corrected_value = {"department": "Compliance"},
        regulation_ref  = "rbi_kyc_master_2016",
        domain          = "kyc",
        comment         = "KYC obligations must route to Compliance dept first.",
    )
    print(f"  [OK] Correction logged: {correction_id[:12]}...")

    # Verify HUMAN_CORRECTION node in chain
    chain = audit.get_chain(chain_id)
    assert chain is not None
    from audit.logger import NodeType
    human_nodes = [n for n in chain.nodes if n.node_type == NodeType.HUMAN_CORRECTION]
    assert len(human_nodes) == 1
    print(f"  [OK] HUMAN_CORRECTION node in audit chain (hash={human_nodes[0].current_hash[:16]}...)")

    # Query ledger
    ledger = CorrectionLedgerClient(audit._worm)
    corrections = await ledger.get_recent_corrections(
        domain="kyc", regulation_ref="rbi_kyc_master_2016"
    )
    assert len(corrections) == 1
    print(f"  [OK] Ledger query: {len(corrections)} correction(s) (exact-first)")
    print(f"       stage={corrections[0]['stage']} | by={corrections[0]['corrected_by']}")

    # Domain fallback (no exact reg_ref match)
    corrections_domain = await ledger.get_recent_corrections(domain="kyc")
    assert len(corrections_domain) >= 1
    print(f"  [OK] Domain fallback query: {len(corrections_domain)} correction(s)")

    # Router with correction injection
    router = HybridRouter(correction_ledger=ledger)
    task = await router.route_with_corrections(
        obligation_id  = "ob-test-001",
        text           = "Banks shall update KYC records annually.",
        domain         = "kyc",
        regulation_ref = "rbi_kyc_master_2016",
    )
    assert task.routing_method == "ledger_corrected"
    assert task.department == "Compliance"
    assert task.confidence == 0.95
    print(f"  [OK] Routing method: {task.routing_method}")
    print(f"  [OK] Routed to: {task.department} (confidence={task.confidence})")
    cid_short = task.metadata.get("correction_id", "")[:12]
    print(f"  [OK] Few-shot correction applied: correction_id={cid_short}...")


asyncio.run(test_feature_5())

print(f"\n{'=' * 60}")
print("  ALL PHASE 9 TESTS PASSED")
print(f"{'=' * 60}")
sys.exit(0)
