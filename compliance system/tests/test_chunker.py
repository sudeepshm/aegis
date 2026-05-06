"""
tests/test_chunker.py
=====================
Unit tests for parser/chunker.py — SemanticChunker and EntityExtractor.
"""

import pytest
from parser.chunker import (
    Entity,
    SemanticChunk,
    ParseResult,
    EntityExtractor,
    SemanticChunker,
    DEFAULT_CHUNK_SIZE,
)


# ──────────────────────────────────────────────────────────────────────────────
# EntityExtractor
# ──────────────────────────────────────────────────────────────────────────────

class TestEntityExtractor:
    """Tests for the rule-based entity extractor."""

    def setup_method(self):
        self.extractor = EntityExtractor()

    def test_extract_regulation_reference(self):
        text = "As per RBI Circular No. RBI/2025-26/42 on KYC norms."
        entities = self.extractor.extract(text)
        reg_entities = [e for e in entities if e.entity_type == "regulation"]
        assert len(reg_entities) >= 1
        assert any("RBI" in e.value for e in reg_entities)

    def test_extract_section_reference(self):
        text = "Under Section 47A of the Banking Regulation Act."
        entities = self.extractor.extract(text)
        sec_entities = [e for e in entities if e.entity_type == "section_ref"]
        assert len(sec_entities) >= 1
        assert any("Section 47" in e.value for e in sec_entities)

    def test_extract_date(self):
        text = "The deadline is April 15, 2025 for compliance."
        entities = self.extractor.extract(text)
        date_entities = [e for e in entities if e.entity_type == "date"]
        assert len(date_entities) >= 1

    def test_extract_organization(self):
        text = "The Reserve Bank of India has issued guidelines."
        entities = self.extractor.extract(text)
        org_entities = [e for e in entities if e.entity_type == "organization"]
        assert len(org_entities) >= 1

    def test_extract_compliance_term(self):
        text = "Banks must comply with KYC and AML regulations."
        entities = self.extractor.extract(text)
        term_entities = [e for e in entities if e.entity_type == "compliance_term"]
        assert len(term_entities) >= 2

    def test_no_entities_in_plain_text(self):
        text = "The weather is nice today."
        entities = self.extractor.extract(text)
        assert len(entities) == 0

    def test_entity_to_dict(self):
        entity = Entity(
            entity_type="regulation",
            value="RBI Circular",
            start_pos=0,
            end_pos=12,
            confidence=0.95,
        )
        d = entity.to_dict()
        assert d["entity_type"] == "regulation"
        assert d["value"] == "RBI Circular"
        assert d["confidence"] == 0.95

    def test_deduplication(self):
        text = "RBI RBI RBI — KYC KYC KYC"
        entities = self.extractor.extract(text)
        # Each unique (type, value) pair should appear only once
        keys = [(e.entity_type, e.value) for e in entities]
        assert len(keys) == len(set(keys))


# ──────────────────────────────────────────────────────────────────────────────
# SemanticChunker
# ──────────────────────────────────────────────────────────────────────────────

class TestSemanticChunker:
    """Tests for the semantic chunker."""

    def setup_method(self):
        self.chunker = SemanticChunker(chunk_size=100, chunk_overlap=10)

    def test_empty_text_returns_empty(self):
        result = self.chunker.chunk("doc-001", "")
        assert result.doc_id == "doc-001"
        assert len(result.chunks) == 0

    def test_whitespace_only_returns_empty(self):
        result = self.chunker.chunk("doc-001", "   \n\n  ")
        assert len(result.chunks) == 0

    def test_short_text_single_chunk(self, sample_regulatory_text):
        # With a large chunk size, the entire text fits in one chunk
        chunker = SemanticChunker(chunk_size=2000, chunk_overlap=0)
        result = chunker.chunk("doc-001", sample_regulatory_text)
        assert result.doc_id == "doc-001"
        assert len(result.chunks) >= 1

    def test_chunks_have_required_fields(self, sample_regulatory_text):
        result = self.chunker.chunk("doc-001", sample_regulatory_text)
        for chunk in result.chunks:
            assert chunk.chunk_id
            assert chunk.doc_id == "doc-001"
            assert chunk.text
            assert chunk.content_hash
            assert chunk.token_count > 0

    def test_chunks_have_entities(self, sample_regulatory_text):
        result = self.chunker.chunk("doc-001", sample_regulatory_text)
        assert result.total_entities > 0
        # At least some chunks should have entities
        chunks_with_entities = [c for c in result.chunks if c.entities]
        assert len(chunks_with_entities) > 0

    def test_chunk_content_hash_is_deterministic(self):
        text = " ".join(["word"] * 100)  # 100 words
        chunker = SemanticChunker(chunk_size=200, chunk_overlap=0)
        r1 = chunker.chunk("doc", text)
        r2 = chunker.chunk("doc", text)
        if r1.chunks and r2.chunks:
            assert r1.chunks[0].content_hash == r2.chunks[0].content_hash

    def test_parse_result_to_dict(self, sample_regulatory_text):
        result = self.chunker.chunk("doc-001", sample_regulatory_text)
        d = result.to_dict()
        assert d["doc_id"] == "doc-001"
        assert "chunk_count" in d
        assert "total_tokens" in d
        assert isinstance(d["chunks"], list)

    def test_sliding_window_overlap(self):
        """Verify overlapping tokens between consecutive chunks."""
        words = " ".join([f"word{i}" for i in range(250)])
        chunker = SemanticChunker(chunk_size=100, chunk_overlap=20)
        result = chunker.chunk("doc", words)
        if len(result.chunks) >= 2:
            # The end of chunk 0 should overlap with start of chunk 1
            c0_words = set(result.chunks[0].text.split()[-20:])
            c1_words = set(result.chunks[1].text.split()[:20])
            overlap = c0_words & c1_words
            assert len(overlap) > 0, "Chunks should have overlapping tokens"


# ──────────────────────────────────────────────────────────────────────────────
# Data structure tests
# ──────────────────────────────────────────────────────────────────────────────

class TestDataStructures:
    def test_semantic_chunk_to_dict(self):
        chunk = SemanticChunk(
            chunk_id="c-001",
            doc_id="d-001",
            text="sample text",
            section="intro",
            token_count=2,
            content_hash="abc123",
        )
        d = chunk.to_dict()
        assert d["chunk_id"] == "c-001"
        assert d["doc_id"] == "d-001"
        assert d["text"] == "sample text"

    def test_parse_result_counts(self):
        result = ParseResult(
            doc_id="d-001",
            chunks=[],
            total_tokens=100,
            total_entities=5,
            sections=["intro", "body"],
        )
        d = result.to_dict()
        assert d["chunk_count"] == 0
        assert d["total_tokens"] == 100
        assert d["sections"] == ["intro", "body"]
