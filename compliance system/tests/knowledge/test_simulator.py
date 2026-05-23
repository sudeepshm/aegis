import pytest
import datetime
from knowledge.simulator import BlastRadiusSimulator

def test_simulator_blast_radius(mock_dag):
    """Test 1: Given a 3-node DAG, delaying the root task by 30 days correctly recalculates the start/end dates for the 2 downstream tasks and triggers a deadline breach flag."""
    sim = BlastRadiusSimulator()

    today = datetime.date.today()
    deadlines = {
        "C": today + datetime.timedelta(days=20)
    }

    report = sim.simulate_delay(
        dag=mock_dag,
        delayed_task="A",
        delay_days=30,
        regulation_deadlines=deadlines,
        task_duration_days=5
    )

    assert report.total_tasks_impacted == 2
    cascade_tasks = {c.task_name: c for c in report.cascade_tasks}
    assert "B" in cascade_tasks
    assert "C" in cascade_tasks

    # A delayed by 30 days -> B delayed by 30 days -> C delayed by 30 days
    assert cascade_tasks["C"].days_delayed == 30

    assert len(report.breached_deadlines) == 1
    assert report.breached_deadlines[0].task_name == "C"
    assert "breach" in report.narrative.lower() or "missed" in report.narrative.lower()
