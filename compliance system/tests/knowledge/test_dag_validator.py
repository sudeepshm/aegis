import pytest
from knowledge.dag_validator import DAGValidator

def test_valid_dag_topological_sort(mock_dag):
    """Test 1: A valid DAG returns True and correctly calculates the topological sort order."""
    validator = DAGValidator()
    result = validator.validate(mock_dag)

    assert result.is_valid is True
    assert result.has_cycles is False
    assert result.topological_order == ["A", "B", "C"]

def test_dag_with_circular_dependency():
    """Test 2: A DAG with a circular dependency (A -> B -> C -> A) successfully raises/returns an error."""
    circular_dag = [
        {"task": "A", "depends_on": ["C"]},
        {"task": "B", "depends_on": ["A"]},
        {"task": "C", "depends_on": ["B"]}
    ]
    validator = DAGValidator()
    result = validator.validate(circular_dag)

    assert result.is_valid is False
    assert result.has_cycles is True
    assert len(result.errors) > 0
    assert any("Circular" in err for err in result.errors)
