import pytest
import hmac
import hashlib
import json
from audit.logger import AuditLogger, AuditLoggerConfig, NodeType

def test_audit_logger_hash_chain():
    """Test 1: Ensure current_hash is correctly calculated using HMAC-SHA256 combining the payload and the previous_hash (WORM immutability check)."""
    config = AuditLoggerConfig(hmac_secret=b"test_secret")
    audit_logger = AuditLogger(config)

    chain_id = audit_logger.new_chain()

    payload_1 = {"data": "first"}
    node_1 = audit_logger.append(chain_id, NodeType.EVIDENCE, payload_1)

    # Check that prev_hash of first node is GENESIS_HASH (64 zeros)
    assert node_1.prev_hash == "0" * 64

    # Calculate expected hash for node 1
    canonical_1 = json.dumps(payload_1, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    message_1 = f"{node_1.prev_hash}{canonical_1}".encode("utf-8")
    expected_hash_1 = hmac.new(b"test_secret", message_1, hashlib.sha256).hexdigest()

    assert node_1.current_hash == expected_hash_1

    # Add second node
    payload_2 = {"data": "second"}
    node_2 = audit_logger.append(chain_id, NodeType.TASK, payload_2)

    # Check that prev_hash of second node is current_hash of first node
    assert node_2.prev_hash == node_1.current_hash

    canonical_2 = json.dumps(payload_2, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    message_2 = f"{node_2.prev_hash}{canonical_2}".encode("utf-8")
    expected_hash_2 = hmac.new(b"test_secret", message_2, hashlib.sha256).hexdigest()

    assert node_2.current_hash == expected_hash_2

    # Verify chain
    is_valid, errors = audit_logger.verify(chain_id)
    assert is_valid is True
    assert len(errors) == 0
