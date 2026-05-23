"""
api/routers/tensions.py
=======================
FastAPI APIRouter for managing TensionRecords (cross-regulatory conflicts).

Endpoints:
  GET   /api/tensions              — list all tensions (filterable)
  GET   /api/tensions/{tension_id} — get a single tension
  PATCH /api/tensions/{tension_id} — update tension status

Data source: Redis (``aegis:tension:*`` keys via ``RedisClient``).

This is an APIRouter — NOT a standalone app.  The caller (``main.py``)
integrates it via ``app.include_router(tensions_router)``.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

logger = logging.getLogger(__name__)

# Redis client
try:
    from core.redis_client import get_redis
    _REDIS_AVAILABLE = True
except ImportError:
    _REDIS_AVAILABLE = False
    logger.warning("Redis unavailable — tensions API will return empty results.")

# TensionRecord schema for validation
try:
    from llm.conflict_detector import TensionRecord
except ImportError:
    TensionRecord = None  # type: ignore[assignment, misc]

# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

router = APIRouter(prefix="/api/tensions", tags=["tensions"])


# ---------------------------------------------------------------------------
# Request/Response models
# ---------------------------------------------------------------------------


class TensionStatusUpdate(BaseModel):
    """Request body for PATCH /api/tensions/{tension_id}."""
    status: str  # "REVIEWED" | "DISMISSED"


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("")
async def list_tensions(
    regulator: str | None = Query(None, description="Filter by regulator (rbi|sebi)"),
    status: str | None = Query(None, description="Filter by status (OPEN|REVIEWED|DISMISSED)"),
    limit: int = Query(50, ge=1, le=500, description="Maximum results to return"),
) -> list[dict[str, Any]]:
    """
    List all tension records.

    Scans ``aegis:tension:*`` keys in Redis, applies optional filters,
    and returns up to ``limit`` results sorted by ``detected_at`` descending.
    """
    if not _REDIS_AVAILABLE:
        return []

    redis = get_redis()
    all_tensions = redis.tension_scan()

    # Apply filters
    filtered = all_tensions
    if regulator:
        filtered = [
            t for t in filtered
            if t.get("new_regulator") == regulator
            or t.get("conflicting_regulator") == regulator
        ]
    if status:
        filtered = [t for t in filtered if t.get("status") == status]

    # Sort by detected_at descending
    filtered.sort(key=lambda t: t.get("detected_at", ""), reverse=True)

    return filtered[:limit]


@router.get("/{tension_id}")
async def get_tension(tension_id: str) -> dict[str, Any]:
    """
    Retrieve a single tension record by ID.

    Returns 404 if the tension is not found or has expired.
    """
    if not _REDIS_AVAILABLE:
        raise HTTPException(status_code=503, detail="Redis unavailable")

    redis = get_redis()
    record = redis.tension_get(tension_id)

    if record is None:
        raise HTTPException(status_code=404, detail=f"Tension {tension_id} not found")

    return record


@router.patch("/{tension_id}")
async def update_tension_status(
    tension_id: str,
    body: TensionStatusUpdate,
) -> dict[str, Any]:
    """
    Update the status of a tension record.

    Valid transitions: OPEN → REVIEWED, OPEN → DISMISSED, REVIEWED → DISMISSED.
    Resets the Redis TTL on update.
    """
    if not _REDIS_AVAILABLE:
        raise HTTPException(status_code=503, detail="Redis unavailable")

    valid_statuses = {"REVIEWED", "DISMISSED"}
    if body.status not in valid_statuses:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid status '{body.status}'. Must be one of: {valid_statuses}",
        )

    redis = get_redis()
    record = redis.tension_get(tension_id)

    if record is None:
        raise HTTPException(status_code=404, detail=f"Tension {tension_id} not found")

    # Update status
    record["status"] = body.status

    # Re-persist with TTL reset
    redis.tension_set(tension_id, record)

    logger.info("Tension %s status updated to %s", tension_id, body.status)
    return record
