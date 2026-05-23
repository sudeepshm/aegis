"""
parser/chunker.py
=================
Semantic chunking and entity extraction for regulatory documents.

Extracted from pdf_parser.py per the architecture specification which
requires a dedicated chunker module.

Pipeline
--------
  1. Heading-aware section splitting
  2. Sliding-window token chunking with overlap
  3. Per-chunk entity enrichment (NER)

Dependencies
------------
  stdlib only (re, hashlib, uuid)
"""

from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional


# ──────────────────────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────────────────────
DEFAULT_CHUNK_SIZE     = 512    # tokens per chunk
DEFAULT_CHUNK_OVERLAP  = 64     # token overlap between consecutive chunks
MAX_CHUNK_SIZE         = 2048   # safety cap
MIN_CHUNK_SIZE         = 10     # discard chunks smaller than this


# ──────────────────────────────────────────────────────────────────────────────
# Data Structures
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class Entity:
    """An extracted regulatory entity."""
    entity_type: str         # e.g., "regulation", "date", "organization", "section_ref"
    value:       str
    start_pos:   int = 0
    end_pos:     int = 0
    confidence:  float = 1.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "entity_type": self.entity_type,
            "value":       self.value,
            "start_pos":   self.start_pos,
            "end_pos":     self.end_pos,
            "confidence":  round(self.confidence, 4),
        }


@dataclass
class SemanticChunk:
    """A semantically coherent text chunk with enrichment metadata."""
    chunk_id:     str
    doc_id:       str
    text:         str
    section:      str = ""
    page_number:  int = 0
    chunk_index:  int = 0
    token_count:  int = 0
    entities:     list[Entity] = field(default_factory=list)
    embedding:    list[float] = field(default_factory=list)
    content_hash: str = ""
    metadata:     dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "chunk_id":     self.chunk_id,
            "doc_id":       self.doc_id,
            "text":         self.text,
            "section":      self.section,
            "page_number":  self.page_number,
            "chunk_index":  self.chunk_index,
            "token_count":  self.token_count,
            "entities":     [e.to_dict() for e in self.entities],
            "content_hash": self.content_hash,
            "metadata":     self.metadata,
        }


@dataclass
class ParseResult:
    """Full output of the parsing/chunking pipeline."""
    doc_id:       str
    chunks:       list[SemanticChunk]
    total_tokens: int = 0
    total_entities: int = 0
    sections:     list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "doc_id":         self.doc_id,
            "chunk_count":    len(self.chunks),
            "total_tokens":   self.total_tokens,
            "total_entities": self.total_entities,
            "sections":       self.sections,
            "chunks":         [c.to_dict() for c in self.chunks],
        }


# ──────────────────────────────────────────────────────────────────────────────
# Entity Extractor
# ──────────────────────────────────────────────────────────────────────────────

class EntityExtractor:
    """
    Rule-based entity extraction for regulatory documents.
    Extracts regulations, dates, section references, and organisations.
    """

    # Patterns for Indian regulatory references
    PATTERNS: list[tuple[str, re.Pattern]] = [
        ("regulation", re.compile(
            r"\b(?:RBI|SEBI|CERT-In|DPDP|IRDAI|PFRDA|NHB)"
            r"(?:\s+(?:Act|Circular|Guideline|Notification|Master Direction|Regulation))"
            r"(?:\s+(?:No\.?\s*)?[\w\-/]+)?",
            re.IGNORECASE
        )),
        ("section_ref", re.compile(
            r"\b(?:Section|Clause|Article|Para(?:graph)?|Rule|Regulation)\s+"
            r"[\d]+(?:\.\d+)*(?:\s*\([a-zA-Z0-9]+\))*",
            re.IGNORECASE
        )),
        ("date", re.compile(
            r"\b\d{1,2}[\-/\.]\d{1,2}[\-/\.]\d{2,4}\b"
            r"|\b(?:January|February|March|April|May|June|July|August"
            r"|September|October|November|December)\s+\d{1,2},?\s+\d{4}\b",
            re.IGNORECASE
        )),
        ("organization", re.compile(
            r"\b(?:Reserve Bank of India|Securities and Exchange Board"
            r"|CERT-In|Ministry of Electronics"
            r"|Insurance Regulatory|NPCI|SEBI|RBI|IRDAI|PFRDA)\b",
            re.IGNORECASE
        )),
        ("compliance_term", re.compile(
            r"\b(?:KYC|AML|CFT|PMLA|FEMA|PPI|NBFC|UCB|SFB"
            r"|CRR|SLR|NPA|LCR|NSFR|IRRBB|ICAAP)\b"
        )),
    ]

    def extract(self, text: str) -> list[Entity]:
        """Extract all entities from the text."""
        entities: list[Entity] = []
        seen: set[tuple[str, str]] = set()

        for entity_type, pattern in self.PATTERNS:
            for match in pattern.finditer(text):
                key = (entity_type, match.group().strip())
                if key not in seen:
                    seen.add(key)
                    entities.append(Entity(
                        entity_type=entity_type,
                        value=match.group().strip(),
                        start_pos=match.start(),
                        end_pos=match.end(),
                        confidence=0.95,
                    ))

        return entities


# ──────────────────────────────────────────────────────────────────────────────
# Semantic Chunker
# ──────────────────────────────────────────────────────────────────────────────

class SemanticChunker:
    """
    Splits raw text into semantically coherent chunks using a combination of:
      1. Heading-aware section splitting
      2. Sliding-window token chunking with overlap
      3. Per-chunk entity enrichment
    """

    # Heading patterns for regulatory documents
    HEADING_PATTERN = re.compile(
        r"^(?:"
        r"(?:#{1,4}\s+.+)"                    # Markdown headings
        r"|(?:(?:CHAPTER|PART|SECTION|ANNEX|APPENDIX|SCHEDULE)\s+[\dIVXLCDM]+)"
        r"|(?:\d+\.\s+[A-Z][A-Za-z\s]+)"      # Numbered headings like "1. Introduction"
        r"|(?:[A-Z][A-Z\s]{4,}$)"              # ALL CAPS lines (likely headings)
        r")",
        re.MULTILINE
    )

    def __init__(self, chunk_size: int = DEFAULT_CHUNK_SIZE,
                 chunk_overlap: int = DEFAULT_CHUNK_OVERLAP):
        self._chunk_size = min(chunk_size, MAX_CHUNK_SIZE)
        self._chunk_overlap = chunk_overlap
        self._entity_extractor = EntityExtractor()

    def chunk(self, doc_id: str, text: str,
              metadata: Optional[dict[str, Any]] = None) -> ParseResult:
        """
        Split text into enriched semantic chunks.

        Returns a ParseResult containing all chunks with entities.
        """
        if not text or not text.strip():
            return ParseResult(doc_id=doc_id, chunks=[])

        # Step 1: Split into sections by headings
        sections = self._split_sections(text)

        # Step 2: Chunk each section
        all_chunks: list[SemanticChunk] = []
        total_tokens = 0
        total_entities = 0
        section_names: list[str] = []
        chunk_index = 0

        for section_title, section_text in sections:
            if section_title:
                section_names.append(section_title)

            sub_chunks = self._sliding_window_chunk(section_text)

            for chunk_text in sub_chunks:
                if len(chunk_text.split()) < MIN_CHUNK_SIZE:
                    continue

                token_count = len(chunk_text.split())  # approximate
                total_tokens += token_count

                # Step 3: Enrich with entities
                entities = self._entity_extractor.extract(chunk_text)
                total_entities += len(entities)

                content_hash = hashlib.sha256(
                    chunk_text.encode("utf-8")
                ).hexdigest()

                chunk = SemanticChunk(
                    chunk_id=str(uuid.uuid4()),
                    doc_id=doc_id,
                    text=chunk_text,
                    section=section_title or "untitled",
                    chunk_index=chunk_index,
                    token_count=token_count,
                    entities=entities,
                    content_hash=content_hash,
                    metadata=metadata or {},
                )
                all_chunks.append(chunk)
                chunk_index += 1

        return ParseResult(
            doc_id=doc_id,
            chunks=all_chunks,
            total_tokens=total_tokens,
            total_entities=total_entities,
            sections=section_names,
        )

    def _split_sections(self, text: str) -> list[tuple[str, str]]:
        """
        Split text into (heading, body) tuples based on heading patterns.

        FIX: If no headings are detected (common in poorly formatted regulatory
        PDFs), fall back to paragraph-level splitting rather than returning the
        entire document as a single 'preamble' block.  This prevents downstream
        chunk loss when the giant preamble is sub-chunked and individual pieces
        fall below MIN_CHUNK_SIZE.
        """
        sections: list[tuple[str, str]] = []
        matches = list(self.HEADING_PATTERN.finditer(text))

        if not matches:
            # No headings found — split on paragraph breaks (double newline)
            paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
            if len(paragraphs) > 1:
                for i, para in enumerate(paragraphs):
                    sections.append((f"paragraph_{i + 1}", para))
                return sections
            # Single solid block of text — return as-is for sliding-window chunker
            return [("", text)]

        # Text before the first heading
        if matches[0].start() > 0:
            preamble = text[:matches[0].start()].strip()
            if preamble:
                sections.append(("preamble", preamble))

        for i, match in enumerate(matches):
            heading = match.group().strip()
            start = match.end()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            body = text[start:end].strip()
            if body:
                sections.append((heading, body))

        return sections


    def _sliding_window_chunk(self, text: str) -> list[str]:
        """Split text into overlapping chunks using a sliding window."""
        words = text.split()
        if len(words) <= self._chunk_size:
            return [text]

        chunks: list[str] = []
        start = 0

        while start < len(words):
            end = min(start + self._chunk_size, len(words))
            chunk_words = words[start:end]
            chunks.append(" ".join(chunk_words))

            if end >= len(words):
                break
            start += self._chunk_size - self._chunk_overlap

        return chunks
