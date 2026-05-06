"""
tests/test_audit_logger.py
===========================
Unit tests for audit/logger.py — HashEngine, WORMStorage, AuditLogger.
"""

import pytest
from pathlib import Path
from audit.logger import (
    GENESIS_HASH, NodeType, AuditNode, AuditChain,
    AuditLoggerConfig, HashEngine, WORMStorage, AuditLogger,
    ExplainabilityMetadata,
)


class TestHashEngine:
    def setup_method(self):
        self.engine = HashEngine(hmac_secret=b"test-secret-key")

    def test_compute_hash_deterministic(self):
        payload = {"key": "value", "num": 42}
        h1 = self.engine.compute_hash(GENESIS_HASH, payload)
        h2 = self.engine.compute_hash(GENESIS_HASH, payload)
        assert h1 == h2

    def test_different_payloads_different_hashes(self):
        h1 = self.engine.compute_hash(GENESIS_HASH, {"a": 1})
        h2 = self.engine.compute_hash(GENESIS_HASH, {"a": 2})
        assert h1 != h2

    def test_different_prev_hash_different_result(self):
        payload = {"key": "value"}
        h1 = self.engine.compute_hash(GENESIS_HASH, payload)
        h2 = self.engine.compute_hash("abc" * 21 + "a", payload)
        assert h1 != h2

    def test_verify_valid_chain(self):
        payload1 = {"step": "ingestion"}
        payload2 = {"step": "parse"}
        h1 = self.engine.compute_hash(GENESIS_HASH, payload1)
        h2 = self.engine.compute_hash(h1, payload2)
        nodes = [
            AuditNode(chain_id="c1", node_index=0, node_type=NodeType.REGULATION,
                      payload=payload1, prev_hash=GENESIS_HASH, current_hash=h1,
                      timestamp="2025-01-01T00:00:00Z"),
            AuditNode(chain_id="c1", node_index=1, node_type=NodeType.MAP,
                      payload=payload2, prev_hash=h1, current_hash=h2,
                      timestamp="2025-01-01T00:01:00Z"),
        ]
        valid, errors = self.engine.verify_chain(nodes)
        assert valid is True
        assert len(errors) == 0

    def test_verify_tampered_chain(self):
        payload = {"step": "ingestion"}
        h1 = self.engine.compute_hash(GENESIS_HASH, payload)
        nodes = [
            AuditNode(chain_id="c1", node_index=0, node_type=NodeType.REGULATION,
                      payload=payload, prev_hash=GENESIS_HASH,
                      current_hash="tampered_hash",
                      timestamp="2025-01-01T00:00:00Z"),
        ]
        valid, errors = self.engine.verify_chain(nodes)
        assert valid is False
        assert len(errors) >= 1

    def test_no_hmac_falls_back_to_sha256(self):
        engine = HashEngine(hmac_secret=None)
        h = engine.compute_hash(GENESIS_HASH, {"test": True})
        assert len(h) == 64  # SHA-256 hex length


class TestWORMStorage:
    def test_in_memory_write_and_read(self):
        worm = WORMStorage()  # no Cosmos connection
        node = AuditNode(
            chain_id="c1", node_index=0, node_type=NodeType.REGULATION,
            payload={"test": True}, prev_hash=GENESIS_HASH,
            current_hash="abc123", timestamp="2025-01-01T00:00:00Z",
        )
        worm.write(node)
        docs = worm.read_chain("c1")
        assert len(docs) == 1
        assert docs[0]["chain_id"] == "c1"

    def test_read_nonexistent_chain(self):
        worm = WORMStorage()
        docs = worm.read_chain("nonexistent")
        assert docs == []


class TestAuditLogger:
    def setup_method(self):
        self.config = AuditLoggerConfig(
            hmac_secret=b"test-secret",
            approved_override_actors={"compliance-officer"},
        )
        self.logger = AuditLogger(self.config)

    def test_new_chain(self):
        chain_id = self.logger.new_chain()
        assert chain_id
        chain = self.logger.get_chain(chain_id)
        assert chain is not None
        assert chain.length == 0

    def test_append_node(self):
        chain_id = self.logger.new_chain()
        node = self.logger.append(chain_id, NodeType.REGULATION,
                                  {"circular": "RBI/2025-26/42"})
        assert node.node_index == 0
        assert node.prev_hash == GENESIS_HASH
        assert node.current_hash

    def test_chain_linking(self):
        chain_id = self.logger.new_chain()
        n0 = self.logger.append(chain_id, NodeType.REGULATION, {"step": 1})
        n1 = self.logger.append(chain_id, NodeType.MAP, {"step": 2})
        assert n1.prev_hash == n0.current_hash

    def test_verify_integrity(self):
        chain_id = self.logger.new_chain()
        self.logger.append(chain_id, NodeType.REGULATION, {"a": 1})
        self.logger.append(chain_id, NodeType.MAP, {"b": 2})
        valid, errors = self.logger.verify(chain_id)
        assert valid is True

    def test_append_to_finalized_raises(self, tmp_path):
        chain_id = self.logger.new_chain()
        self.logger.append(chain_id, NodeType.REGULATION, {"test": True})
        self.logger.finalize(chain_id, tmp_path)
        with pytest.raises(ValueError, match="finalized"):
            self.logger.append(chain_id, NodeType.MAP, {"fail": True})

    def test_signoff_requires_approved_actor(self):
        chain_id = self.logger.new_chain()
        self.logger.append(chain_id, NodeType.REGULATION, {"test": True})
        with pytest.raises(PermissionError):
            self.logger.append(chain_id, NodeType.SIGNOFF,
                               {"approved": True}, actor="random-user")
        # Approved actor should work
        node = self.logger.append(chain_id, NodeType.SIGNOFF,
                                  {"approved": True}, actor="compliance-officer")
        assert node.node_type == NodeType.SIGNOFF

    def test_nonexistent_chain_raises(self):
        with pytest.raises(ValueError):
            self.logger.append("nonexistent", NodeType.REGULATION, {})

    def test_finalize_generates_reports(self, tmp_path):
        chain_id = self.logger.new_chain()
        self.logger.append(chain_id, NodeType.REGULATION, {"circular": "test"})
        reports = self.logger.finalize(chain_id, tmp_path)
        assert "xbrl" in reports or "pdf" in reports

    def test_explainability_metadata(self):
        chain_id = self.logger.new_chain()
        explain = ExplainabilityMetadata(
            decision_id="d-001", model_version="v1.0",
            feature_weights={"confidence": 0.8}, confidence_score=0.92,
            reasoning_steps=["Step 1", "Step 2"],
            alternative_decisions=[{"action": "escalate"}],
        )
        node = self.logger.append(chain_id, NodeType.MAP,
                                  {"map_id": "m1"}, explainability=explain)
        assert node.explainability is not None
        assert node.explainability.confidence_score == 0.92

    def test_traceability_graph(self):
        chain_id = self.logger.new_chain()
        self.logger.append(chain_id, NodeType.REGULATION, {"a": 1})
        self.logger.append(chain_id, NodeType.MAP, {"b": 2})
        graph = self.logger.get_traceability_graph(chain_id)
        assert graph["chain_id"] == chain_id
        assert len(graph["nodes"]) == 2
        assert len(graph["edges"]) == 1
