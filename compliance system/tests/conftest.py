import pytest
import sys
from pathlib import Path

# Ensure the project root is on sys.path for module imports
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from typing import Dict, Any, List
import networkx as nx

@pytest.fixture
def mock_pipeline_state() -> Dict[str, Any]:
    """Mock PipelineState object as dictionary for tests."""
    return {
        "document_id": "test-doc-001",
        "current_task": "extract",
        "extracted_obligations": [],
        "risk_score": 0.0,
        "anomalies": [],
        "evidence": [],
        "status": "in_progress",
        "metadata": {}
    }

@pytest.fixture
def mock_dag() -> List[Dict[str, Any]]:
    """Mock DAG representing a list of tasks with dependencies."""
    return [
        {"task": "A", "depends_on": [], "duration_days": 5},
        {"task": "B", "depends_on": ["A"], "duration_days": 10},
        {"task": "C", "depends_on": ["B"], "duration_days": 15}
    ]

@pytest.fixture
def mock_ontology() -> nx.DiGraph:
    """Mock NetworkX ComplianceOntology with 3 nodes."""
    G = nx.DiGraph()
    G.add_node("RBI", type="regulator", jurisdiction="IN")
    G.add_node("DPDP", type="regulation", jurisdiction="IN")
    G.add_node("GDPR", type="regulation", jurisdiction="EU")
    G.add_edge("RBI", "DPDP", relationship="enforces")
    return G

@pytest.fixture
def sample_regulatory_text() -> str:
    """Sample regulatory circular text for testing."""
    return (
        "RBI Circular No. RBI/2025-26/42 dated April 15, 2025\n\n"
        "Subject: Master Direction on KYC and AML/CFT Guidelines\n\n"
        "1. Introduction\n"
        "All banks shall implement the revised Know Your Customer (KYC) norms "
        "as prescribed under the Prevention of Money Laundering Act (PMLA). "
        "Banks must ensure that customer due diligence (CDD) procedures are "
        "applied to all new and existing accounts.\n\n"
        "2. Encryption Requirements\n"
        "All systems processing customer data shall use AES-256 encryption "
        "for data at rest and TLS 1.3 for data in transit. Banks are required "
        "to implement multi-factor authentication (MFA) and role-based access "
        "control (RBAC) across all critical systems.\n\n"
        "3. Cyber Security Framework\n"
        "Banks shall comply with the CERT-In cyber security guidelines. "
        "All cyber incidents must be reported within 6 hours of detection. "
        "A mandatory Security Operations Centre (SOC) shall be operational "
        "24x7 for real-time monitoring.\n\n"
        "4. NPA Classification\n"
        "Non-performing assets shall be classified as per IRAC norms. "
        "Banks should ensure quarterly provisioning review. Capital adequacy "
        "ratio (CRAR) must be maintained above the prescribed threshold.\n\n"
        "5. Compliance Timeline\n"
        "All banks are directed to implement these changes by "
        "September 30, 2025. Non-compliance may attract penalties "
        "under Section 47A of the Banking Regulation Act, 1949.\n"
    )

@pytest.fixture
def sample_doc_id() -> str:
    return "doc-test-001"

@pytest.fixture
def sample_obligation_text() -> str:
    """A single obligation sentence."""
    return (
        "All banks shall implement AES-256 encryption for all customer data "
        "at rest as per RBI Circular No. RBI/2025-26/42."
    )
