"""
llm/conflict_detector.py
========================
Cross-Regulatory Conflict Detection — Project Aegis Phase 3

This module provides:
  1. ``TensionRecord`` dataclass — the canonical schema for conflicts
  2. ``stage_conflict_detection()`` — a LangGraph node that runs after
     ``guardrails_validation`` to detect semantic contradictions between
     newly extracted obligations and existing obligations in Qdrant.

Detection pipeline (per obligation):
  1. Embed the obligation text (reuses the embedding client from pdf_parser)
  2. Query Qdrant for top-K nearest neighbours where ``regulator != current``
  3. If cosine similarity ≥ threshold, invoke GPT-4o for conflict classification
  4. Store confirmed conflicts as ``TensionRecord`` objects in Redis

Dependencies:
  pip install qdrant-client==1.9.1
"""

from __future__ import annotations

import json
import logging
import os
import uuid
import time
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Optional imports — module degrades gracefully
# ---------------------------------------------------------------------------
try:
    from qdrant_client import QdrantClient
    from qdrant_client.models import Filter, FieldCondition, MatchValue
    _QDRANT_AVAILABLE = True
except ImportError:
    _QDRANT_AVAILABLE = False
    logger.warning("qdrant-client not installed. Conflict detection will be skipped.")

try:
    from langchain_google_genai import ChatGoogleGenerativeAI
    from langchain_core.messages import HumanMessage, SystemMessage
    _LLM_AVAILABLE = True
except ImportError:
    _LLM_AVAILABLE = False

try:
    from core.redis_client import get_redis
    _REDIS_AVAILABLE = True
except ImportError:
    _REDIS_AVAILABLE = False


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

QDRANT_URL = os.environ.get("QDRANT_URL", "http://localhost:6333")
QDRANT_COLLECTION = os.environ.get("QDRANT_COLLECTION", "aegis_chunks")
CONFLICT_SIMILARITY_THRESHOLD = float(
    os.environ.get("CONFLICT_SIMILARITY_THRESHOLD", "0.88")
)
CONFLICT_TOP_K = int(os.environ.get("CONFLICT_TOP_K", "5"))


# ---------------------------------------------------------------------------
# TensionRecord
# ---------------------------------------------------------------------------


@dataclass
class TensionRecord:
    """A confirmed or candidate regulatory conflict between two obligations."""

    tension_id: str             # UUID v4
    new_obligation_id: str      # from validated_map.obligations[n].obligation_id
    new_chunk_id: str           # PipelineState.chunk_id
    new_text: str               # obligation raw_text
    new_regulator: str          # "rbi" | "sebi"
    conflicting_chunk_id: str   # Qdrant point id of the hit
    conflicting_text: str       # Qdrant payload.text of the hit
    conflicting_regulator: str  # Qdrant payload.regulator of the hit
    similarity_score: float     # Qdrant cosine score, e.g. 0.912
    conflict_type: str          # "DIRECT_CONTRADICTION" | "PARTIAL_OVERLAP" | "TEMPORAL_CONFLICT"
    llm_explanation: str        # GPT-4o one-sentence rationale
    detected_at: str            # ISO-8601
    status: str = "OPEN"        # "OPEN" | "REVIEWED" | "DISMISSED"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "TensionRecord":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


# ---------------------------------------------------------------------------
# Conflict Classification Prompt
# ---------------------------------------------------------------------------

CONFLICT_SYSTEM_PROMPT = (
    "You are a regulatory conflict analyst. You answer ONLY in JSON.\n"
    "You compare two regulatory obligations and determine if they contradict each other."
)

CONFLICT_USER_TEMPLATE = (
    'Regulation A: "{new_text}"\n'
    'Regulation B: "{existing_text}"\n'
    "Do these two obligations contradict each other?\n"
    'Respond: {{"verdict": "YES|NO", '
    '"conflict_type": "DIRECT_CONTRADICTION|PARTIAL_OVERLAP|TEMPORAL_CONFLICT|NONE", '
    '"explanation": "<one sentence>"}}'
)


# ---------------------------------------------------------------------------
# LLM Conflict Classifier
# ---------------------------------------------------------------------------


def _classify_conflict(new_text: str, existing_text: str) -> dict[str, Any] | None:
    """
    Invoke GPT-4o to classify whether two obligation texts conflict.

    Returns parsed JSON dict or None on failure.
    """
    if not _LLM_AVAILABLE:
        return None

    try:
        import re
        api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
        llm = ChatGoogleGenerativeAI(model="gemini-1.5-pro-latest", temperature=0.0, google_api_key=api_key)
        system = SystemMessage(content=CONFLICT_SYSTEM_PROMPT)
        human = HumanMessage(content=CONFLICT_USER_TEMPLATE.format(
            new_text=new_text[:500],
            existing_text=existing_text[:500],
        ))
        response = llm.invoke([system, human])
        # Parse JSON from response
        raw = response.content.strip()
        cleaned = re.sub(r"```(?:json)?", "", raw).strip().strip("`").strip()
        return json.loads(cleaned)
    except Exception as exc:
        logger.warning("Conflict classification LLM call failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Qdrant Query
# ---------------------------------------------------------------------------


def _query_qdrant(
    embedding: list[float],
    current_regulator: str,
) -> list[dict[str, Any]]:
    """
    Search Qdrant for semantically similar chunks from a DIFFERENT regulator.

    Returns list of dicts with keys: id, score, text, regulator, chunk_id.
    """
    if not _QDRANT_AVAILABLE:
        return []

    try:
        client = QdrantClient(url=QDRANT_URL)
        results = client.search(
            collection_name=QDRANT_COLLECTION,
            query_vector=embedding,
            limit=CONFLICT_TOP_K,
            query_filter=Filter(
                must_not=[
                    FieldCondition(
                        key="regulator",
                        match=MatchValue(value=current_regulator),
                    )
                ]
            ),
            score_threshold=CONFLICT_SIMILARITY_THRESHOLD,
        )

        hits = []
        for point in results:
            payload = point.payload or {}
            hits.append({
                "id": str(point.id),
                "score": point.score,
                "text": payload.get("text", ""),
                "regulator": payload.get("regulator", "unknown"),
                "chunk_id": payload.get("chunk_id", str(point.id)),
            })
        return hits
    except Exception as exc:
        logger.warning("Qdrant query failed: %s", exc)
        return []


# ---------------------------------------------------------------------------
# Embedding helper
# ---------------------------------------------------------------------------


def _get_embedding(text: str) -> list[float] | None:
    """
    Get embedding vector for text using OpenAI embeddings.

    Falls back to None if the embedding client is unavailable.
    """
    try:
        from langchain_openai import OpenAIEmbeddings
        embeddings = OpenAIEmbeddings(model="text-embedding-3-small")
        return embeddings.embed_query(text[:8000])
    except Exception as exc:
        logger.warning("Embedding generation failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# LangGraph Node: stage_conflict_detection
# ---------------------------------------------------------------------------


def stage_conflict_detection(state: dict[str, Any]) -> dict[str, Any]:
    """
    LangGraph node — runs after ``guardrails_validation``.

    For each obligation in the validated map:
      1. Generate embedding
      2. Query Qdrant for cross-regulator matches above threshold
      3. Classify each high-similarity hit with GPT-4o
      4. Store confirmed conflicts as TensionRecords in Redis

    Adds ``conflict_results: list[dict]`` to pipeline state.
    Always returns state — never raises.
    """
    logger.info("Stage 6 | Conflict Detection — start")

    conflict_results: list[dict[str, Any]] = []

    validated_map = state.get("validated_map")
    if not validated_map:
        logger.warning("Stage 6 | No validated_map — skipping conflict detection")
        return {**state, "conflict_results": conflict_results}

    chunk_id = state.get("chunk_id", "unknown")
    # Determine regulator from context (default to "rbi")
    current_regulator = state.get("regulator", "rbi")

    obligations = validated_map.get("obligations", [])
    logger.info("Stage 6 | Checking %d obligation(s) for conflicts", len(obligations))

    for ob in obligations:
        ob_id = ob.get("obligation_id", "unknown")
        raw_text = ob.get("raw_text", "")
        if not raw_text:
            continue

        # 1. Generate embedding
        embedding = _get_embedding(raw_text)
        if embedding is None:
            logger.debug("Stage 6 | Skipping obligation %s — no embedding", ob_id[:8])
            continue

        # 2. Query Qdrant
        hits = _query_qdrant(embedding, current_regulator)
        if not hits:
            continue

        logger.info(
            "Stage 6 | Obligation %s has %d high-similarity hits",
            ob_id[:8], len(hits),
        )

        # 3. Classify each hit
        for hit in hits:
            classification = _classify_conflict(raw_text, hit["text"])
            if classification is None:
                continue

            verdict = classification.get("verdict", "NO").upper()
            if verdict != "YES":
                continue

            conflict_type = classification.get("conflict_type", "PARTIAL_OVERLAP")
            explanation = classification.get("explanation", "")

            # 4. Create TensionRecord
            tension = TensionRecord(
                tension_id=str(uuid.uuid4()),
                new_obligation_id=ob_id,
                new_chunk_id=chunk_id,
                new_text=raw_text[:1000],
                new_regulator=current_regulator,
                conflicting_chunk_id=hit["chunk_id"],
                conflicting_text=hit["text"][:1000],
                conflicting_regulator=hit["regulator"],
                similarity_score=hit["score"],
                conflict_type=conflict_type,
                llm_explanation=explanation,
                detected_at=datetime.now(timezone.utc).isoformat(),
                status="OPEN",
            )

            conflict_results.append(tension.to_dict())

            # Store in Redis
            if _REDIS_AVAILABLE:
                try:
                    get_redis().tension_set(tension.tension_id, tension.to_dict())
                except Exception:
                    pass  # Redis failure must never break the pipeline

            logger.info(
                "Stage 6 | CONFLICT DETECTED: %s (%s) — %s vs %s (score=%.3f)",
                tension.tension_id[:8],
                conflict_type,
                current_regulator,
                hit["regulator"],
                hit["score"],
            )

    logger.info(
        "Stage 6 | Conflict Detection complete — %d conflict(s) found",
        len(conflict_results),
    )
    time.sleep(10)
    return {**state, "conflict_results": conflict_results}
