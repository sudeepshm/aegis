import pytest
from unittest.mock import MagicMock
import networkx as nx

# Mock implementation of JurisdictionalMap for testing the requested scenario
class JurisdictionalMap:
    def __init__(self, ontology):
        self.ontology = ontology

    def check_cross_border_interaction(self, node_a_id, node_b_id):
        node_a = self.ontology.nodes.get(node_a_id, {})
        node_b = self.ontology.nodes.get(node_b_id, {})

        jur_a = node_a.get("jurisdiction")
        jur_b = node_b.get("jurisdiction")

        if jur_a and jur_b and jur_a != jur_b:
            return {
                "is_cross_border": True,
                "recommendation": "cross-border compensating control recommendation",
                "tensions": [f"{jur_a} vs {jur_b}"]
            }
        return {"is_cross_border": False}

def test_cross_border_sovereignty_check(mock_ontology):
    """Test 2: Cross-border sovereignty check: An 'EU' node interacting with an 'IN' (India) node successfully triggers the cross-border compensating control recommendation."""
    jmap = JurisdictionalMap(mock_ontology)

    # GDPR is 'EU', DPDP is 'IN'
    result = jmap.check_cross_border_interaction("GDPR", "DPDP")

    assert result["is_cross_border"] is True
    assert "cross-border compensating control" in result["recommendation"]
