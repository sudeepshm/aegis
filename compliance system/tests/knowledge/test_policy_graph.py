import pytest
from unittest.mock import MagicMock
import datetime
from knowledge.policy_graph import PolicyGraph
from knowledge.compliance_ontology import RegNode

def test_policy_tension_verb_conflict():
    """Test 1: Ingesting a new obligation with the verb 'delete' successfully triggers a POLICY_TENSION against an existing node mapped with the verb 'retain'."""
    mock_onto = MagicMock()

    # Create an existing node with verb "retain" and object "data"
    existing_node = RegNode(
        id="node_1",
        reg_id="REG_001",
        source="RBI",
        title="Data Retention Policy",
        effective_date=datetime.date(2020, 1, 1),
        expiry_date=None,
        mandatory=True,
        subject="Banks",
        domain="data_protection",
        summary="Organizations must retain customer data for 5 years.",
        clauses=[]
    )

    mock_onto.get_all_nodes.return_value = [existing_node]

    pg = PolicyGraph(ontology=mock_onto)

    # Obligation with verb 'delete' and object 'data'
    obligation_text = "You must delete customer data immediately."

    report = pg.detect_policy_tension(
        obligation_text=obligation_text,
        regulation_ref="NEW_REG",
        domain="data_protection"
    )

    assert report.has_tension is True
    assert any(pair.verb_a == "erase" and pair.verb_b == "retain" for pair in report.tension_pairs)
    assert "POLICY TENSION detected" in report.summary
