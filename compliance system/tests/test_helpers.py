"""
tests/test_helpers.py
======================
Unit tests for utils/helpers.py — hashing, timestamps, retry, masking, schema.
"""

import pytest
import json
from pathlib import Path
from datetime import datetime, timezone
from utils.helpers import (
    hash_bytes, hash_string, hash_file, hash_payload,
    verify_file_integrity, now_ist, utc_to_ist, format_iso8601,
    parse_iso8601, mask_dict, mask_string, mask_payload_for_logging,
    validate_payload, load_json_string, SchemaValidationError,
)


class TestHashing:
    def test_hash_bytes(self):
        h = hash_bytes(b"hello")
        assert len(h) == 64
        assert h == hash_bytes(b"hello")  # deterministic

    def test_hash_string(self):
        h = hash_string("hello")
        assert len(h) == 64

    def test_hash_payload_deterministic(self):
        p = {"b": 2, "a": 1}
        h1 = hash_payload(p)
        h2 = hash_payload({"a": 1, "b": 2})
        assert h1 == h2  # sorted keys

    def test_hash_file(self, tmp_path):
        f = tmp_path / "test.txt"
        f.write_text("hello world")
        h = hash_file(f)
        assert len(h) == 64

    def test_hash_file_not_found(self):
        with pytest.raises(FileNotFoundError):
            hash_file("/nonexistent/file.txt")

    def test_verify_integrity_pass(self, tmp_path):
        f = tmp_path / "test.txt"
        f.write_text("integrity check")
        h = hash_file(f)
        assert verify_file_integrity(f, h) is True

    def test_verify_integrity_fail(self, tmp_path):
        f = tmp_path / "test.txt"
        f.write_text("integrity check")
        assert verify_file_integrity(f, "wrong_hash") is False


class TestTimestamps:
    def test_now_ist_is_aware(self):
        dt = now_ist()
        assert dt.tzinfo is not None

    def test_utc_to_ist(self):
        utc = datetime.now(tz=timezone.utc)
        ist = utc_to_ist(utc)
        assert ist.tzinfo is not None
        offset = ist.utcoffset().total_seconds()
        assert offset == 5.5 * 3600

    def test_utc_to_ist_naive_raises(self):
        with pytest.raises(ValueError):
            utc_to_ist(datetime.now())

    def test_format_iso8601(self):
        ts = format_iso8601()
        assert "+" in ts or "-" in ts

    def test_parse_iso8601(self):
        dt = parse_iso8601("2025-01-15T10:30:00+05:30")
        assert dt.year == 2025


class TestMasking:
    def test_mask_dict_redacts_sensitive(self):
        data = {"email": "test@example.com", "name": "Alice"}
        masked = mask_dict(data)
        assert masked["email"] == "***REDACTED***"
        assert masked["name"] == "Alice"

    def test_mask_dict_nested(self):
        data = {"user": {"password": "secret123", "name": "Bob"}}
        masked = mask_dict(data)
        assert masked["user"]["password"] == "***REDACTED***"

    def test_mask_string_email(self):
        result = mask_string("Contact alice@example.com for info")
        assert "[EMAIL]" in result
        assert "alice@example.com" not in result

    def test_mask_string_phone(self):
        result = mask_string("Call 9876543210 now")
        assert "[PHONE]" in result

    def test_mask_payload_for_logging(self):
        data = {"email": "test@example.com", "notes": "Call 9876543210"}
        masked = mask_payload_for_logging(data)
        assert masked["email"] == "***REDACTED***"
        assert "[PHONE]" in masked["notes"]


class TestSchemaValidation:
    def test_valid_payload(self):
        schema = {"type": "object", "required": ["name"],
                  "properties": {"name": {"type": "string"}}}
        errors = validate_payload({"name": "Alice"}, schema, raise_on_error=False)
        assert errors == []

    def test_missing_required(self):
        schema = {"type": "object", "required": ["name"]}
        errors = validate_payload({}, schema, raise_on_error=False)
        assert any("required" in e for e in errors)

    def test_wrong_type(self):
        schema = {"type": "object", "properties": {"age": {"type": "integer"}}}
        errors = validate_payload({"age": "not_int"}, schema, raise_on_error=False)
        assert any("type" in e for e in errors)

    def test_raises_on_error(self):
        schema = {"type": "object", "required": ["id"]}
        with pytest.raises(SchemaValidationError):
            validate_payload({}, schema, raise_on_error=True)

    def test_load_json_string(self):
        data = load_json_string('{"key": "value"}')
        assert data["key"] == "value"
