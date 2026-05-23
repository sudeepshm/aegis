"""
chunking/semantic_chunker.py
-----------------------------
Semantic Chunking Engine for Regulatory Documents (RBI / India Central)

Pipeline
--------
  1. Accept the normalized JSON payload from fetch_rbi.py (ingestion pipeline)
  2. Semantically split long regulations into atomic chunks:
       Obligation | Definition | Exception | Clause | Preamble | Schedule | Penalty
  3. Enrich each chunk with metadata tags:
       regulation_type, affected_departments, urgency_score, …
  4. Compute Azure OpenAI embeddings (text-embedding-3-large) and format output
     for Azure AI Search *and* Qdrant — 300–500 token window with 15% overlap
  5. Full lineage tracking: content hash, source_url, parser_version, chunk_index

Environment Variables
---------------------
  AZURE_OPENAI_ENDPOINT         – e.g. https://<res>.openai.azure.com/
  AZURE_OPENAI_API_KEY          – Azure OpenAI key
  AZURE_OPENAI_EMBEDDING_MODEL  – deployment name (default: text-embedding-3-large)
  CHUNKER_PARSER_VERSION        – semver string stamped into lineage (default: 1.0.0)
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Sequence

import tiktoken
from openai import AzureOpenAI

logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
)

# ---------------------------------------------------------------------------
# Constants & Configuration
# ---------------------------------------------------------------------------

PARSER_VERSION: str = os.environ.get("CHUNKER_PARSER_VERSION", "1.0.0")

# Token window: target 400 tokens, ±100 tolerance, ~15% overlap
TARGET_CHUNK_TOKENS = 400
MIN_CHUNK_TOKENS = 300
MAX_CHUNK_TOKENS = 500
OVERLAP_TOKENS = 60          # ~15 % of 400

# Azure OpenAI embedding model (3072-dim for text-embedding-3-large)
DEFAULT_EMBEDDING_MODEL = "text-embedding-3-large"
EMBEDDING_DIMENSIONS = 3072

# Batch size for embedding API calls (max 2048 strings per request)
EMBEDDING_BATCH_SIZE = 32

# Retry config for transient Azure OpenAI errors
MAX_RETRIES = 4
RETRY_BASE_DELAY = 1.5  # seconds (exponential back-off)

# Tiktoken encoding used by text-embedding-3-* models
TIKTOKEN_ENCODING = "cl100k_base"

# ---------------------------------------------------------------------------
# Semantic taxonomy
# ---------------------------------------------------------------------------

# Maps chunk type to the regex patterns that signal its start.
# Patterns are matched against section headings or the first sentence of a block.
CHUNK_TYPE_PATTERNS: dict[str, list[re.Pattern[str]]] = {
    "definition": [
        re.compile(r"\b(means?|refers?\s+to|is\s+defined\s+as|shall\s+mean|'[^']+'\s+means?)\b", re.I),
        re.compile(r"^\s*\"[A-Z][^\"]+\"\s+means?", re.I),
        re.compile(r"^(definitions?|glossary|interpretation)\b", re.I),
    ],
    "obligation": [
        re.compile(r"\b(shall|must|is\s+required\s+to|are\s+required\s+to|obligated\s+to|mandated\s+to)\b", re.I),
        re.compile(r"^(every\s+(bank|entity|nbfc|re|regulated\s+entity)|no\s+(bank|person|entity))\b", re.I),
    ],
    "exception": [
        re.compile(r"\b(provided\s+that|except|notwithstanding|save\s+as|not\s+apply|exempted?\s+from|unless\s+otherwise)\b", re.I),
        re.compile(r"^(exception|proviso|provided|save|however)\b", re.I),
    ],
    "penalty": [
        re.compile(r"\b(penalty|fine|punishable|imprisonment|liable\s+to\s+pay|compounding)\b", re.I),
        re.compile(r"^(enforcement|penal|consequence|sanction)\b", re.I),
    ],
    "schedule": [
        re.compile(r"^(schedule|annexure|appendix|exhibit|form)\s+[A-Z0-9\-]+", re.I),
    ],
    "preamble": [
        re.compile(r"^(whereas|preamble|background|in\s+exercise\s+of|in\s+pursuance\s+of)", re.I),
    ],
}

FALLBACK_CHUNK_TYPE = "clause"

# ---------------------------------------------------------------------------
# Department keyword taxonomy
# ---------------------------------------------------------------------------

DEPARTMENT_KEYWORDS: dict[str, list[str]] = {
    "treasury":         ["liquidity", "slr", "clr", "cash reserve", "g-sec", "government securities", "repo", "reverse repo"],
    "compliance":       ["compliance", "audit", "regulatory reporting", "returns", "filing", "kyc", "aml", "cft"],
    "risk_management":  ["capital adequacy", "car", "tier 1", "tier 2", "npa", "provisioning", "credit risk", "market risk", "operational risk"],
    "retail_banking":   ["savings account", "current account", "fixed deposit", "retail", "customer", "branch", "atm"],
    "credit":           ["loan", "lending", "credit facility", "npa", "restructuring", "moratorium", "exposure"],
    "it_operations":    ["cyber", "information security", "data", "system", "outsourcing", "cloud", "it framework"],
    "forex":            ["foreign exchange", "fema", "ecb", "nri", "export", "import", "remittance", "forex"],
    "payments":         ["payment", "prepaid", "wallet", "upi", "neft", "rtgs", "settlement", "clearing"],
    "hr":               ["employee", "staff", "workforce", "training", "grievance", "whistle"],
    "legal":            ["court", "litigation", "statute", "act", "ordinance", "gazette", "notif"],
}

# ---------------------------------------------------------------------------
# Urgency scoring keywords
# ---------------------------------------------------------------------------

URGENCY_SIGNALS: dict[str, int] = {
    # High urgency (score 8-10)
    "immediate effect":      10,
    "with immediate effect": 10,
    "emergency":             10,
    "urgent":                 9,
    "forthwith":              9,
    "within 30 days":         8,
    "within 15 days":         9,
    "within 7 days":         10,
    # Medium urgency (4-7)
    "within 90 days":         6,
    "within 60 days":         7,
    "by march 31":            6,
    "within one year":        5,
    "within six months":      5,
    "shall ensure":           4,
    # Low urgency (1-3)
    "may":                    1,
    "should":                 2,
    "recommended":            2,
    "advisory":               3,
    "guidance":               2,
}

REGULATION_TYPE_PATTERNS: dict[str, list[str]] = {
    "master_direction":   ["master direction", "master directions"],
    "master_circular":    ["master circular"],
    "notification":       ["notification", "rbi/", "dbr.", "dcbr.", "dnbr."],
    "circular":           ["circular", "a.p.(dir series)"],
    "press_release":      ["press release", "press statement"],
    "faq":                ["frequently asked", "faq"],
    "report":             ["annual report", "bi-monthly", "monetary policy"],
    "guideline":          ["guidelines", "guideline"],
    "framework":          ["framework"],
}

# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass
class ChunkLineage:
    """Full audit trail for a single chunk."""
    document_id: str
    source_url: str
    parser_version: str
    chunk_index: int           # 0-based position within the document
    chunk_hash: str            # SHA-256 of the chunk text
    document_hash: str         # SHA-256 of the full raw_text (from ingestion)
    ingested_at: str           # ISO-8601 timestamp from ingestion pipeline
    chunked_at: str            # ISO-8601 timestamp of this chunking run


@dataclass
class ChunkMetadata:
    """Enriched regulatory metadata for a chunk."""
    regulation_type: str
    affected_departments: list[str]
    urgency_score: float       # 0.0 – 10.0
    chunk_type: str            # obligation | definition | exception | clause | …
    section_heading: str       # nearest parent heading, or ""
    token_count: int
    char_count: int
    regulator: str
    published_date: str


@dataclass
class SemanticChunk:
    """A single atomic chunk ready for vector DB ingestion."""
    chunk_id: str              # UUID v4, stable per run
    text: str
    metadata: ChunkMetadata
    lineage: ChunkLineage
    embedding: list[float] = field(default_factory=list)

    def to_azure_search_doc(self) -> dict[str, Any]:
        """Serialize for Azure AI Search (flat schema with @search.action)."""
        return {
            "@search.action": "mergeOrUpload",
            "id":                   self.chunk_id,
            "text":                 self.text,
            "embedding":            self.embedding,
            # metadata fields (flattened — Azure Search doesn't nest well)
            "regulation_type":      self.metadata.regulation_type,
            "affected_departments": self.metadata.affected_departments,
            "urgency_score":        self.metadata.urgency_score,
            "chunk_type":           self.metadata.chunk_type,
            "section_heading":      self.metadata.section_heading,
            "token_count":          self.metadata.token_count,
            "char_count":           self.metadata.char_count,
            "regulator":            self.metadata.regulator,
            "published_date":       self.metadata.published_date,
            # lineage
            "document_id":          self.lineage.document_id,
            "source_url":           self.lineage.source_url,
            "parser_version":       self.lineage.parser_version,
            "chunk_index":          self.lineage.chunk_index,
            "chunk_hash":           self.lineage.chunk_hash,
            "document_hash":        self.lineage.document_hash,
            "ingested_at":          self.lineage.ingested_at,
            "chunked_at":           self.lineage.chunked_at,
        }

    def to_qdrant_point(self) -> dict[str, Any]:
        """Serialize for Qdrant REST API / Python client PointStruct."""
        return {
            "id":      self.chunk_id,
            "vector":  self.embedding,
            "payload": {
                "text":                 self.text,
                "regulation_type":      self.metadata.regulation_type,
                "affected_departments": self.metadata.affected_departments,
                "urgency_score":        self.metadata.urgency_score,
                "chunk_type":           self.metadata.chunk_type,
                "section_heading":      self.metadata.section_heading,
                "token_count":          self.metadata.token_count,
                "char_count":           self.metadata.char_count,
                "regulator":            self.metadata.regulator,
                "published_date":       self.metadata.published_date,
                "document_id":          self.lineage.document_id,
                "source_url":           self.lineage.source_url,
                "parser_version":       self.lineage.parser_version,
                "chunk_index":          self.lineage.chunk_index,
                "chunk_hash":           self.lineage.chunk_hash,
                "document_hash":        self.lineage.document_hash,
                "ingested_at":          self.lineage.ingested_at,
                "chunked_at":           self.lineage.chunked_at,
            },
        }


# ---------------------------------------------------------------------------
# Tokenizer (shared singleton)
# ---------------------------------------------------------------------------

_ENCODER: tiktoken.Encoding | None = None


def _get_encoder() -> tiktoken.Encoding:
    global _ENCODER
    if _ENCODER is None:
        _ENCODER = tiktoken.get_encoding(TIKTOKEN_ENCODING)
    return _ENCODER


def _count_tokens(text: str) -> int:
    return len(_get_encoder().encode(text))


def _encode(text: str) -> list[int]:
    return _get_encoder().encode(text)


def _decode(token_ids: list[int]) -> str:
    return _get_encoder().decode(token_ids)


# ---------------------------------------------------------------------------
# Semantic Splitter
# ---------------------------------------------------------------------------


class SemanticSplitter:
    """
    Splits regulatory text into atomic semantic chunks using a hybrid strategy:

    Stage 1 – Structural split
        Regex-based detection of numbered clauses, lettered sub-clauses,
        section headings (ALL CAPS, Roman numerals, decimal notation).

    Stage 2 – Semantic boundary detection
        Classify each structural block into: Obligation / Definition /
        Exception / Clause / Penalty / Schedule / Preamble.
        Adjacent blocks of the *same type* are merged up to MAX_CHUNK_TOKENS.

    Stage 3 – Token-window enforcement
        Blocks that exceed MAX_CHUNK_TOKENS are recursively split on sentence
        boundaries with OVERLAP_TOKENS of sliding context preserved.
    """

    # Patterns for structural heading detection
    _HEADING_PATTERNS: list[re.Pattern[str]] = [
        re.compile(r"^(?:CHAPTER|PART|SECTION|ARTICLE)\s+[IVXLC\d]+", re.M),
        re.compile(r"^\d+(?:\.\d+)*\s+[A-Z]", re.M),          # 3.1 Some Heading
        re.compile(r"^[A-Z][A-Z\s]{4,}$", re.M),               # ALL CAPS HEADING
        re.compile(r"^\((?:[ivxlc]+|[a-z]|\d+)\)\s+", re.M),   # (i), (a), (1)
        re.compile(r"^\d+\.\s+", re.M),                         # 1. Clause
    ]

    # Sentence boundary splitter (handles abbreviations reasonably)
    _SENTENCE_SPLIT = re.compile(
        r"(?<=[.!?])\s+(?=[A-Z\"\(])|(?<=\:)\s+(?=[A-Z\"\(])"
    )

    def split(self, raw_text: str, section_headings: list[str]) -> list[tuple[str, str]]:
        """
        Returns a list of (text_block, nearest_heading) tuples.
        Text blocks respect MIN_CHUNK_TOKENS ≤ tokens ≤ MAX_CHUNK_TOKENS.
        """
        # Step 1: structural segmentation
        blocks = self._structural_split(raw_text, section_headings)

        # Step 2: merge tiny adjacent same-type blocks
        blocks = self._merge_by_type(blocks)

        # Step 3: enforce token ceiling with overlap
        final_blocks: list[tuple[str, str]] = []
        for text, heading in blocks:
            if _count_tokens(text) <= MAX_CHUNK_TOKENS:
                if _count_tokens(text) >= MIN_CHUNK_TOKENS or len(text.strip()) > 80:
                    final_blocks.append((text.strip(), heading))
            else:
                final_blocks.extend(self._sliding_window_split(text, heading))

        return [b for b in final_blocks if b[0].strip()]

    # ------------------------------------------------------------------
    # Stage 1 — Structural split
    # ------------------------------------------------------------------

    def _structural_split(
        self, text: str, headings: list[str]
    ) -> list[tuple[str, str]]:
        """Split text at heading / clause boundaries."""
        heading_set = set(h.strip() for h in headings)
        lines = text.splitlines(keepends=True)

        blocks: list[tuple[str, str]] = []
        current_lines: list[str] = []
        current_heading = ""

        for line in lines:
            stripped = line.strip()

            is_heading = (
                stripped in heading_set
                or any(p.match(stripped) for p in self._HEADING_PATTERNS)
            )

            if is_heading and current_lines:
                block_text = "".join(current_lines).strip()
                if block_text:
                    blocks.append((block_text, current_heading))
                current_lines = [line]
                current_heading = stripped
            else:
                current_lines.append(line)

        if current_lines:
            block_text = "".join(current_lines).strip()
            if block_text:
                blocks.append((block_text, current_heading))

        return blocks

    # ------------------------------------------------------------------
    # Stage 2 — Merge adjacent same-type tiny blocks
    # ------------------------------------------------------------------

    def _merge_by_type(
        self, blocks: list[tuple[str, str]]
    ) -> list[tuple[str, str]]:
        if not blocks:
            return blocks

        merged: list[tuple[str, str]] = []
        acc_text, acc_heading = blocks[0]
        acc_type = classify_chunk_type(acc_text, acc_heading)

        for text, heading in blocks[1:]:
            this_type = classify_chunk_type(text, heading)
            combined = acc_text + "\n\n" + text
            combined_tokens = _count_tokens(combined)

            if this_type == acc_type and combined_tokens <= MAX_CHUNK_TOKENS:
                acc_text = combined
                # Keep the earlier heading unless empty
                acc_heading = acc_heading or heading
            else:
                merged.append((acc_text, acc_heading))
                acc_text, acc_heading, acc_type = text, heading, this_type

        merged.append((acc_text, acc_heading))
        return merged

    # ------------------------------------------------------------------
    # Stage 3 — Sliding-window sentence split for oversized blocks
    # ------------------------------------------------------------------

    def _sliding_window_split(
        self, text: str, heading: str
    ) -> list[tuple[str, str]]:
        sentences = self._SENTENCE_SPLIT.split(text)
        token_ids_list = [_encode(s) for s in sentences]

        chunks: list[tuple[str, str]] = []
        window: list[int] = []
        overlap_buffer: list[int] = []  # tail of last window for overlap

        for token_ids in token_ids_list:
            # If adding this sentence exceeds the ceiling, flush
            if window and len(window) + len(token_ids) > MAX_CHUNK_TOKENS:
                chunk_text = _decode(window).strip()
                if _count_tokens(chunk_text) >= MIN_CHUNK_TOKENS:
                    chunks.append((chunk_text, heading))
                # Start new window with overlap
                window = overlap_buffer + token_ids
                overlap_buffer = window[-OVERLAP_TOKENS:]
            else:
                window.extend(token_ids)
                overlap_buffer = window[-OVERLAP_TOKENS:]

        # Final window
        if window:
            chunk_text = _decode(window).strip()
            if chunk_text:
                chunks.append((chunk_text, heading))

        return chunks


# ---------------------------------------------------------------------------
# Chunk type classifier
# ---------------------------------------------------------------------------


def classify_chunk_type(text: str, heading: str = "") -> str:
    """
    Classify a text block into one of the semantic chunk types.
    Scoring: each pattern match adds weight; highest wins.
    """
    combined = f"{heading}\n{text[:600]}"  # only inspect first ~600 chars
    scores: dict[str, int] = {t: 0 for t in CHUNK_TYPE_PATTERNS}

    for chunk_type, patterns in CHUNK_TYPE_PATTERNS.items():
        for pat in patterns:
            if pat.search(combined):
                scores[chunk_type] += 1

    best_type = max(scores, key=lambda k: scores[k])
    if scores[best_type] == 0:
        return FALLBACK_CHUNK_TYPE
    return best_type


# ---------------------------------------------------------------------------
# Metadata enricher
# ---------------------------------------------------------------------------


class MetadataEnricher:
    """
    Derives regulation_type, affected_departments, and urgency_score
    from the chunk text and document-level signals.
    """

    def enrich(
        self,
        chunk_text: str,
        title: str,
        chunk_type: str,
        section_heading: str,
        published_date: str,
        regulator: str,
        token_count: int,
    ) -> ChunkMetadata:
        combined_signal = f"{title} {section_heading} {chunk_text}".lower()

        regulation_type = self._detect_regulation_type(title + " " + chunk_text)
        departments = self._detect_departments(combined_signal)
        urgency = self._compute_urgency(combined_signal, regulation_type, published_date)

        return ChunkMetadata(
            regulation_type=regulation_type,
            affected_departments=departments,
            urgency_score=urgency,
            chunk_type=chunk_type,
            section_heading=section_heading,
            token_count=token_count,
            char_count=len(chunk_text),
            regulator=regulator,
            published_date=published_date,
        )

    def _detect_regulation_type(self, text: str) -> str:
        text_lower = text.lower()
        for reg_type, keywords in REGULATION_TYPE_PATTERNS.items():
            for kw in keywords:
                if kw in text_lower:
                    return reg_type
        return "circular"  # sensible default for RBI

    def _detect_departments(self, text_lower: str) -> list[str]:
        matched: list[str] = []
        for dept, keywords in DEPARTMENT_KEYWORDS.items():
            if any(kw in text_lower for kw in keywords):
                matched.append(dept)
        return matched or ["general"]

    def _compute_urgency(
        self, text_lower: str, regulation_type: str, published_date: str
    ) -> float:
        score = 0.0
        for signal, weight in URGENCY_SIGNALS.items():
            if signal in text_lower:
                score = max(score, weight)

        # Bump score for certain regulation types
        type_boost = {
            "notification": 1.0,
            "master_direction": 0.5,
            "framework": 0.5,
            "circular": 0.3,
        }
        score += type_boost.get(regulation_type, 0.0)

        # Time decay: older documents are less urgent
        try:
            pub_dt = datetime.fromisoformat(published_date.replace("Z", "+00:00"))
            age_days = (datetime.now(timezone.utc) - pub_dt).days
            if age_days > 365:
                score *= 0.6
            elif age_days > 180:
                score *= 0.8
        except Exception:
            pass

        return round(min(score, 10.0), 2)


# ---------------------------------------------------------------------------
# Lineage tracker
# ---------------------------------------------------------------------------


def build_lineage(
    document_id: str,
    source_url: str,
    raw_text: str,
    chunk_text: str,
    chunk_index: int,
    ingested_at: str,
) -> ChunkLineage:
    return ChunkLineage(
        document_id=document_id,
        source_url=source_url,
        parser_version=PARSER_VERSION,
        chunk_index=chunk_index,
        chunk_hash=hashlib.sha256(chunk_text.encode("utf-8")).hexdigest(),
        document_hash=hashlib.sha256(raw_text.encode("utf-8")).hexdigest(),
        ingested_at=ingested_at,
        chunked_at=datetime.now(timezone.utc).isoformat(),
    )


# ---------------------------------------------------------------------------
# Azure OpenAI Embedding Client
# ---------------------------------------------------------------------------


class EmbeddingClient:
    """
    Wraps Azure OpenAI embeddings with batching and exponential back-off.

    Produces 3072-dim vectors from text-embedding-3-large.
    """

    def __init__(self) -> None:
        self._client = AzureOpenAI(
            azure_endpoint=_env("AZURE_OPENAI_ENDPOINT"),
            api_key=_env("AZURE_OPENAI_API_KEY"),
            api_version="2024-02-01",
        )
        self._model = os.environ.get(
            "AZURE_OPENAI_EMBEDDING_MODEL", DEFAULT_EMBEDDING_MODEL
        )

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """
        Embed a list of texts.  Returns a list of float vectors in the same
        order as the input.  Processes in batches of EMBEDDING_BATCH_SIZE.
        """
        all_vectors: list[list[float]] = []

        for batch_start in range(0, len(texts), EMBEDDING_BATCH_SIZE):
            batch = texts[batch_start: batch_start + EMBEDDING_BATCH_SIZE]
            vectors = self._embed_with_retry(batch)
            all_vectors.extend(vectors)

        return all_vectors

    def _embed_with_retry(self, texts: list[str]) -> list[list[float]]:
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                response = self._client.embeddings.create(
                    input=texts,
                    model=self._model,
                    dimensions=EMBEDDING_DIMENSIONS,
                )
                # API guarantees order matches input
                return [item.embedding for item in sorted(response.data, key=lambda x: x.index)]
            except Exception as exc:
                if attempt == MAX_RETRIES:
                    raise RuntimeError(
                        f"Embedding API failed after {MAX_RETRIES} attempts: {exc}"
                    ) from exc
                delay = RETRY_BASE_DELAY * (2 ** (attempt - 1))
                logger.warning(
                    "Embedding attempt %d/%d failed (%s). Retrying in %.1fs…",
                    attempt, MAX_RETRIES, exc, delay,
                )
                time.sleep(delay)
        return []  # unreachable


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _env(key: str, default: str | None = None) -> str:
    value = os.environ.get(key, default)
    if value is None:
        raise EnvironmentError(
            f"Required environment variable '{key}' is not set."
        )
    return value


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Main Chunking Engine
# ---------------------------------------------------------------------------


class SemanticChunkingEngine:
    """
    Accepts the normalized ingestion payload (dict) and produces a list of
    SemanticChunk objects ready for vector DB upsert.

    Usage
    -----
    engine = SemanticChunkingEngine()
    chunks = engine.process(ingestion_payload)

    # Azure AI Search
    search_docs = [c.to_azure_search_doc() for c in chunks]

    # Qdrant
    qdrant_points = [c.to_qdrant_point() for c in chunks]
    """

    def __init__(self, embed: bool = True) -> None:
        self._splitter = SemanticSplitter()
        self._enricher = MetadataEnricher()
        self._embed_client = EmbeddingClient() if embed else None
        self._embed = embed

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def process(self, payload: dict[str, Any]) -> list[SemanticChunk]:
        """
        Process a single ingestion payload document.

        Parameters
        ----------
        payload : dict
            The normalized JSON document produced by fetch_rbi.py.
            Required keys: document_id, regulator, source_url,
                           published_date, title, extracted_content,
                           ingested_at.

        Returns
        -------
        list[SemanticChunk]
            One SemanticChunk per atomic unit, with embeddings populated
            if embed=True was set at construction.
        """
        self._validate_payload(payload)

        document_id: str = payload["document_id"]
        regulator: str = payload["regulator"]
        source_url: str = payload["source_url"]
        published_date: str = payload["published_date"]
        title: str = payload["title"]
        ingested_at: str = payload.get("ingested_at", _utcnow_iso())

        extracted: dict[str, Any] = payload["extracted_content"]
        raw_text: str = extracted.get("raw_text", "")
        section_headings: list[str] = extracted.get("section_headings", [])
        tables: list[dict[str, Any]] = extracted.get("tables", [])

        if not raw_text.strip():
            logger.warning("document_id=%s has empty raw_text. Skipping.", document_id)
            return []

        logger.info(
            "Chunking document_id=%s | tokens=%d | headings=%d | tables=%d",
            document_id,
            _count_tokens(raw_text),
            len(section_headings),
            len(tables),
        )

        # -------------------------------------------------------------------
        # 1. Semantic split
        # -------------------------------------------------------------------
        text_blocks: list[tuple[str, str]] = self._splitter.split(raw_text, section_headings)

        # Append table blocks (each table → one chunk, unless over token limit)
        table_blocks = self._serialize_tables(tables)
        text_blocks.extend(table_blocks)

        logger.info("Produced %d raw blocks before enrichment.", len(text_blocks))

        # -------------------------------------------------------------------
        # 2. Enrich + build SemanticChunk objects (without embeddings yet)
        # -------------------------------------------------------------------
        chunks: list[SemanticChunk] = []
        for idx, (block_text, heading) in enumerate(text_blocks):
            token_count = _count_tokens(block_text)
            chunk_type = classify_chunk_type(block_text, heading)

            metadata = self._enricher.enrich(
                chunk_text=block_text,
                title=title,
                chunk_type=chunk_type,
                section_heading=heading,
                published_date=published_date,
                regulator=regulator,
                token_count=token_count,
            )

            lineage = build_lineage(
                document_id=document_id,
                source_url=source_url,
                raw_text=raw_text,
                chunk_text=block_text,
                chunk_index=idx,
                ingested_at=ingested_at,
            )

            chunks.append(
                SemanticChunk(
                    chunk_id=str(uuid.uuid4()),
                    text=block_text,
                    metadata=metadata,
                    lineage=lineage,
                )
            )

        # -------------------------------------------------------------------
        # 3. Batch embed
        # -------------------------------------------------------------------
        if self._embed and self._embed_client and chunks:
            chunks = self._attach_embeddings(chunks)

        logger.info(
            "Chunking complete: document_id=%s → %d chunks (embedded=%s)",
            document_id,
            len(chunks),
            self._embed,
        )
        return chunks

    def process_batch(self, payloads: list[dict[str, Any]]) -> list[SemanticChunk]:
        """Process multiple ingestion payloads."""
        all_chunks: list[SemanticChunk] = []
        for payload in payloads:
            try:
                all_chunks.extend(self.process(payload))
            except Exception as exc:
                doc_id = payload.get("document_id", "unknown")
                logger.error("Failed to chunk document_id=%s: %s", doc_id, exc)
        return all_chunks

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_payload(payload: dict[str, Any]) -> None:
        required = {"document_id", "regulator", "source_url", "published_date",
                    "title", "extracted_content", "ingested_at"}
        missing = required - payload.keys()
        if missing:
            raise ValueError(f"Ingestion payload missing required keys: {missing}")
        ec = payload["extracted_content"]
        if "raw_text" not in ec:
            raise ValueError("extracted_content must contain 'raw_text'.")

    @staticmethod
    def _serialize_tables(tables: list[dict[str, Any]]) -> list[tuple[str, str]]:
        """Convert structured table dicts into plain-text chunks."""
        result: list[tuple[str, str]] = []
        for tbl in tables:
            rows: list[list[str]] = tbl.get("rows", [])
            if not rows:
                continue
            lines = []
            for row in rows:
                lines.append(" | ".join(str(cell) for cell in row))
            table_text = "\n".join(lines)
            heading = f"Table {tbl.get('table_index', '')}".strip()
            # Only add if within token budget; else skip (very wide tables)
            if _count_tokens(table_text) <= MAX_CHUNK_TOKENS * 2:
                result.append((table_text, heading))
        return result

    def _attach_embeddings(self, chunks: list[SemanticChunk]) -> list[SemanticChunk]:
        texts = [c.text for c in chunks]
        logger.info("Requesting embeddings for %d chunks…", len(texts))

        try:
            vectors = self._embed_client.embed_batch(texts)  # type: ignore[union-attr]
        except Exception as exc:
            logger.error("Embedding batch failed: %s. Chunks returned without embeddings.", exc)
            return chunks

        for chunk, vector in zip(chunks, vectors):
            chunk.embedding = vector

        return chunks


# ---------------------------------------------------------------------------
# Serialization helpers
# ---------------------------------------------------------------------------


def chunks_to_jsonl(chunks: list[SemanticChunk], target: str = "qdrant") -> str:
    """
    Serialize chunks to newline-delimited JSON.

    Parameters
    ----------
    chunks : list[SemanticChunk]
    target : "qdrant" | "azure_search"
    """
    lines: list[str] = []
    for chunk in chunks:
        if target == "azure_search":
            doc = chunk.to_azure_search_doc()
        else:
            doc = chunk.to_qdrant_point()
        lines.append(json.dumps(doc, ensure_ascii=False))
    return "\n".join(lines)


def chunks_to_dict_list(
    chunks: list[SemanticChunk], target: str = "qdrant"
) -> list[dict[str, Any]]:
    if target == "azure_search":
        return [c.to_azure_search_doc() for c in chunks]
    return [c.to_qdrant_point() for c in chunks]


# ---------------------------------------------------------------------------
# CLI / local smoke-test entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage: python semantic_chunker.py <path_to_ingestion_payload.json> [--no-embed]")
        sys.exit(1)

    payload_path = sys.argv[1]
    do_embed = "--no-embed" not in sys.argv

    with open(payload_path, encoding="utf-8") as fh:
        raw_payload = json.load(fh)

    # Support both a single dict and a list of dicts
    payloads: list[dict[str, Any]] = (
        raw_payload if isinstance(raw_payload, list) else [raw_payload]
    )

    engine = SemanticChunkingEngine(embed=do_embed)
    all_chunks: list[SemanticChunk] = []
    for p in payloads:
        all_chunks.extend(engine.process(p))

    output_path = payload_path.replace(".json", "_chunks.jsonl")
    with open(output_path, "w", encoding="utf-8") as fh:
        fh.write(chunks_to_jsonl(all_chunks, target="qdrant"))

    print(f"✓ {len(all_chunks)} chunks written to {output_path}")

    # Print summary stats
    type_counts: dict[str, int] = {}
    for c in all_chunks:
        t = c.metadata.chunk_type
        type_counts[t] = type_counts.get(t, 0) + 1

    print("\nChunk type distribution:")
    for t, n in sorted(type_counts.items(), key=lambda x: -x[1]):
        print(f"  {t:<20} {n:>4}")

    token_counts = [c.metadata.token_count for c in all_chunks]
    if token_counts:
        print(f"\nToken stats — min: {min(token_counts)} | "
              f"max: {max(token_counts)} | "
              f"avg: {sum(token_counts)//len(token_counts)}")