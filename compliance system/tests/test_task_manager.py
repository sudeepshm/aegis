"""
tests/test_task_manager.py
===========================
Unit tests for workflow/task_manager.py — SLAEngine, DependencyResolver, TaskManager.
"""

import pytest
from datetime import datetime, timedelta, timezone
from workflow.task_manager import (
    TaskState, EscalationLevel, SLARecord, ComplianceTask,
    SLAEngine, DependencyResolver, EscalationEngine, TaskManager,
)


class TestSLAEngine:
    def setup_method(self):
        self.engine = SLAEngine()

    def test_start_sla(self):
        sla = self.engine.start_sla(24)
        assert sla.sla_hours == 24
        assert sla.breached is False
        assert sla.remaining_seconds > 0

    def test_breach_detection_expired(self):
        sla = SLARecord(
            sla_hours=1,
            started_at=datetime.now(tz=timezone.utc) - timedelta(hours=2),
            deadline=datetime.now(tz=timezone.utc) - timedelta(hours=1),
        )
        assert self.engine.check_breach(sla) is True

    def test_no_breach_when_active(self):
        sla = self.engine.start_sla(24)
        assert self.engine.check_breach(sla) is False

    def test_pause_and_resume(self):
        sla = self.engine.start_sla(24)
        self.engine.pause_sla(sla)
        assert sla.paused is True
        self.engine.resume_sla(sla)
        assert sla.paused is False

    def test_sla_to_dict(self):
        sla = self.engine.start_sla(8)
        d = sla.to_dict()
        assert d["sla_hours"] == 8
        assert "remaining_sec" in d


class TestDependencyResolver:
    def setup_method(self):
        self.resolver = DependencyResolver()

    def test_ready_tasks_no_deps(self):
        self.resolver.add_task("t1", [])
        self.resolver.add_task("t2", [])
        ready = self.resolver.get_ready_tasks(set())
        assert "t1" in ready
        assert "t2" in ready

    def test_ready_tasks_with_deps(self):
        self.resolver.add_task("t1", [])
        self.resolver.add_task("t2", ["t1"])
        ready = self.resolver.get_ready_tasks(set())
        assert "t1" in ready
        assert "t2" not in ready
        ready_after = self.resolver.get_ready_tasks({"t1"})
        assert "t2" in ready_after

    def test_circular_detection(self):
        self.resolver.add_task("t1", ["t2"])
        self.resolver.add_task("t2", ["t1"])
        assert self.resolver.has_circular() is True

    def test_no_circular(self):
        self.resolver.add_task("t1", [])
        self.resolver.add_task("t2", ["t1"])
        assert self.resolver.has_circular() is False


class TestEscalationEngine:
    def setup_method(self):
        self.engine = EscalationEngine()

    def test_no_escalation_when_no_sla(self):
        task = ComplianceTask(
            task_id="t1", obligation_id="o1", department="Compliance",
            assignee="Alice", assignee_email="a@b.com",
            description="Test", priority="2-High",
        )
        assert self.engine.check_escalation(task) is None

    def test_escalation_when_breached(self):
        sla = SLARecord(
            sla_hours=1,
            started_at=datetime.now(tz=timezone.utc) - timedelta(hours=2),
            deadline=datetime.now(tz=timezone.utc) - timedelta(hours=1),
        )
        task = ComplianceTask(
            task_id="t1", obligation_id="o1", department="Compliance",
            assignee="Alice", assignee_email="a@b.com",
            description="Test", priority="1-Critical", sla=sla,
        )
        level = self.engine.check_escalation(task)
        assert level is not None


class TestTaskManager:
    def setup_method(self):
        # No ServiceNow connection — tests run in-memory
        self.manager = TaskManager()

    def test_create_task(self):
        task = self.manager.create_task(
            obligation_id="o1", department="Compliance",
            assignee="Alice", assignee_email="a@b.com",
            description="Test compliance task", priority="2-High",
            sla_hours=24,
        )
        assert task.task_id
        assert task.state == TaskState.NEW
        assert task.sla is not None

    def test_transition_state(self):
        task = self.manager.create_task(
            obligation_id="o1", department="IT", assignee="Bob",
            assignee_email="b@b.com", description="Test", priority="3-Medium",
        )
        updated = self.manager.transition(task.task_id, TaskState.IN_PROGRESS, "Alice")
        assert updated.state == TaskState.IN_PROGRESS
        assert len(updated.history) >= 2

    def test_transition_nonexistent_raises(self):
        with pytest.raises(ValueError):
            self.manager.transition("nonexistent", TaskState.IN_PROGRESS)

    def test_get_task(self):
        task = self.manager.create_task(
            obligation_id="o1", department="Finance", assignee="Carol",
            assignee_email="c@b.com", description="Test", priority="4-Low",
        )
        found = self.manager.get_task(task.task_id)
        assert found is not None
        assert found.task_id == task.task_id

    def test_task_to_dict(self):
        task = self.manager.create_task(
            obligation_id="o1", department="Compliance", assignee="Dan",
            assignee_email="d@b.com", description="Test", priority="1-Critical",
        )
        d = task.to_dict()
        assert d["department"] == "Compliance"
        assert d["state"] == "New"
