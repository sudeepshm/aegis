"""
tests/test_validator.py
========================
Unit tests for validation/validator.py — HashGuard and data structures.
"""

import pytest
from datetime import datetime, timedelta, timezone
from validation.validator import (
    HashGuard, CheckCategory, CheckResult, ValidationOutcome,
    ValidationRequest, ValidationReport, PASS_CONFIDENCE_THRESHOLD,
)


class TestHashGuard:
    def setup_method(self):
        self.guard = HashGuard()

    def test_first_submission_passes(self):
        content = b"unique document content " + str(datetime.now()).encode()
        sha256, result = self.guard.check(content)
        assert len(sha256) == 64
        assert result.passed is True
        assert result.confidence >= 0.90

    def test_duplicate_detection(self):
        content = b"duplicate test content for validator"
        _, r1 = self.guard.check(content)
        assert r1.passed is True
        _, r2 = self.guard.check(content)
        assert r2.passed is False
        assert "DUPLICATE" in r2.explanation

    def test_backdate_detection(self):
        content = b"backdated test " + str(datetime.now()).encode()
        future = datetime.now(tz=timezone.utc) + timedelta(hours=1)
        _, result = self.guard.check(content, claimed_timestamp=future)
        assert result.passed is False
        assert "BACKDATED" in result.explanation

    def test_valid_timestamp_passes(self):
        content = b"valid timestamp " + str(datetime.now()).encode()
        now = datetime.now(tz=timezone.utc)
        _, result = self.guard.check(content, claimed_timestamp=now)
        assert result.passed is True


class TestCheckResult:
    def test_to_dict(self):
        r = CheckResult(
            check_id="c1", category=CheckCategory.HASH_GUARD,
            name="test_check", passed=True, confidence=0.95,
            explanation="All good", evidence={"key": "val"},
        )
        d = r.to_dict()
        assert d["check_id"] == "c1"
        assert d["category"] == "HASH_GUARD"
        assert d["passed"] is True


class TestValidationRequest:
    def test_defaults(self):
        req = ValidationRequest(
            task_id="t1", obligation_id="o1", department="IT",
            unit_head_email="a@b.com", unit_head_name="Alice",
        )
        assert req.run_esb_checks is True
        assert req.run_kv_checks is True
        assert req.run_doc_checks is True
        assert req.previous_doc_version == (2, 0)
