import pytest
from agents.verifier_agent import VerifierAgent, AmbiguityReport
from agents.critic_agent import CriticAgent, CritiqueReport
from agents.planner_agent import PlannerAgent, _resolve_disagreement

def test_verifier_ambiguity():
    """Test 1: Verifier flags 'robust' as vague, returns Ambiguity Score > 0.1."""
    agent = VerifierAgent()
    report = agent.verify("Implement robust encryption.")

    assert report.ambiguity_score >= 0.15
    assert any(f.excerpt == "robust" for f in report.flags)

def test_critic_evidence_rejection():
    """Test 2: Critic rejects 'screenshot' and requests alternative."""
    agent = CriticAgent()
    flags = agent._evidence_challenger(["screenshot"])

    assert len(flags) == 1
    assert flags[0]["evidence_type"] == "screenshot"
    assert "alternative" in flags[0]

def test_planner_disagreement_resolution():
    """Test 3: Planner resolves disagreement (Ambiguity=HIGH and Conflict=HIGH) by escalating."""
    amb = AmbiguityReport(obligation_text="test", ambiguity_score=0.9)
    crit = CritiqueReport(obligation_text="test", regulation_ref="ref", critique_passed=False)

    resolution_note, needs_3rd_pass = _resolve_disagreement(amb, crit)

    assert "Escalating" in resolution_note
    assert needs_3rd_pass is False
