"""
tests/test_router.py
====================
Unit tests for routing/router.py — RuleEngine, MLClassifier, HybridRouter.
"""

import pytest
from routing.router import (
    Priority, RuleEngine, MLClassifier, HybridRouter,
    OrganisationHierarchy, CONFIDENCE_THRESHOLD,
)


class TestRuleEngine:
    def setup_method(self):
        self.engine = RuleEngine()

    def test_kyc_matches_operations(self):
        dept, conf = self.engine.match("KYC verification requirements")
        assert dept == "operations"
        assert conf >= 0.90

    def test_aml_matches_compliance(self):
        dept, conf = self.engine.match("Anti-money laundering monitoring")
        assert dept == "compliance"
        assert conf >= 0.90

    def test_encryption_matches_it_security(self):
        dept, conf = self.engine.match("TLS encryption cyber security")
        assert dept == "it-security"

    def test_npa_matches_credit_risk(self):
        dept, conf = self.engine.match("NPA classification non-performing")
        assert dept == "credit-risk"

    def test_no_match_returns_none(self):
        dept, conf = self.engine.match("The weather is nice today")
        assert dept is None
        assert conf == 0.0


class TestMLClassifier:
    def setup_method(self):
        self.clf = MLClassifier()

    def test_security_text_classifies_to_it_security(self):
        dept, conf = self.clf.classify("security encryption vulnerability patch")
        assert dept == "it-security"

    def test_unknown_text_defaults_to_compliance(self):
        dept, conf = self.clf.classify("xyz abc")
        assert dept == "compliance"
        assert conf >= 0.50


class TestOrganisationHierarchy:
    def setup_method(self):
        self.hierarchy = OrganisationHierarchy()

    def test_get_existing_department(self):
        dept = self.hierarchy.get("compliance")
        assert dept is not None
        assert dept.name == "Compliance"

    def test_get_nonexistent_returns_none(self):
        assert self.hierarchy.get("nonexistent") is None

    def test_escalation_chain(self):
        chain = self.hierarchy.get_escalation_chain("it-security")
        assert len(chain) >= 2
        assert chain[0].dept_id == "it-security"

    def test_sla_resolution(self):
        sla = self.hierarchy.resolve_sla("compliance", "1-Critical")
        assert sla == 4


class TestHybridRouter:
    def setup_method(self):
        self.router = HybridRouter()

    def test_routes_kyc_to_operations(self):
        task = self.router.route("ob-001", "KYC customer due diligence", "HIGH")
        assert task.department == "Operations"
        assert task.priority == Priority.CRITICAL

    def test_routes_unknown_text_escalated(self):
        task = self.router.route("ob-002", "something completely unrelated xyz", "LOW")
        if task.confidence < CONFIDENCE_THRESHOLD:
            assert task.escalated is True
            assert task.department == "Compliance"

    def test_batch_routing(self):
        items = [
            {"obligation_id": "o1", "text": "KYC norms", "risk_level": "HIGH"},
            {"obligation_id": "o2", "text": "Encryption TLS", "risk_level": "MEDIUM"},
        ]
        results = self.router.route_batch(items)
        assert len(results) == 2

    def test_escalation_chain(self):
        chain = self.router.get_escalation_chain("IT Security")
        assert len(chain) >= 1

    def test_routed_task_has_fields(self):
        task = self.router.route("ob-001", "AML suspicious transaction monitoring")
        assert task.task_id
        assert task.sla_hours > 0
        d = task.to_dict()
        assert "department" in d
        assert "priority" in d
