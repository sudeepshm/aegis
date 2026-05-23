"""
api/pipeline_runner.py
======================
Async pipeline orchestrator for Project Aegis Phase 4.

Provides ``run_pipeline_async()`` which drives the LangGraph 5+1 stage
pipeline with an optional event callback that broadcasts WebSocket events
to connected clients in real-time.

This module does NOT replace the existing synchronous ``process_chunk()``
in ``map_generator.py``. It is a new async entrypoint used exclusively
by the WebSocket pipeline endpoint.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

# Type alias for the WebSocket broadcast callback
EventCallback = Callable[[dict[str, Any]], Awaitable[None]]


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def run_pipeline_async(
    chunk_text: str,
    chunk_id: str | None = None,
    job_id: str | None = None,
    callback: EventCallback | None = None,
    regulator: str = "rbi",
) -> dict[str, Any]:
    """
    Run the obligation extraction pipeline asynchronously with stage-level
    event broadcasting.

    Parameters
    ----------
    chunk_text : str
        Raw text to process.
    chunk_id : str, optional
        Identifier for the chunk (auto-generated if omitted).
    job_id : str, optional
        Job identifier for WebSocket event correlation.
    callback : EventCallback, optional
        Async function called at STARTED/COMPLETED/ERROR transitions.
    regulator : str
        Source regulator for conflict detection context.

    Returns
    -------
    dict
        The final validated (and conflict-checked) obligation map.
    """
    from llm.map_generator import (
        PipelineState,
        stage_obligation_extraction,
        stage_action_decomposition,
        stage_sme_rule_injection,
        stage_json_structuring,
        stage_guardrails_validation,
        handle_error,
        _new_id,
    )

    # Optional conflict detection import
    try:
        from llm.conflict_detector import stage_conflict_detection
        _has_conflict = True
    except ImportError:
        _has_conflict = False

    if not chunk_text or not chunk_text.strip():
        raise ValueError("chunk_text must be a non-empty string.")

    _chunk_id = chunk_id or str(uuid.uuid4())
    _job_id = job_id or str(uuid.uuid4())

    state: dict[str, Any] = {
        "chunk_text": chunk_text,
        "chunk_id": _chunk_id,
        "error": None,
        "regulator": regulator,
    }

    # Stage definitions: (name, function)
    stages = [
        ("obligation_extraction", stage_obligation_extraction),
        ("action_decomposition", stage_action_decomposition),
        ("sme_rule_injection", stage_sme_rule_injection),
        ("json_structuring", stage_json_structuring),
        ("guardrails_validation", stage_guardrails_validation),
    ]
    if _has_conflict:
        stages.append(("conflict_detector", stage_conflict_detection))

    async def _emit(event: dict[str, Any]) -> None:
        if callback:
            event.setdefault("job_id", _job_id)
            event.setdefault("timestamp", _utcnow_iso())
            try:
                await callback(event)
            except Exception as exc:
                logger.warning("Event callback failed: %s", exc)

    logger.info("Pipeline started: job_id=%s chunk_id=%s", _job_id, _chunk_id)

    for stage_name, stage_fn in stages:
        await _emit({
            "stage": stage_name,
            "status": "STARTED",
            "log": f"Stage '{stage_name}' started",
        })

        try:
            state = stage_fn(state)
            await _emit({
                "stage": stage_name,
                "status": "COMPLETED",
                "log": f"Stage '{stage_name}' completed",
                "payload": _safe_payload(state, stage_name),
            })
        except Exception as exc:
            logger.exception("Pipeline stage '%s' failed", stage_name)
            await _emit({
                "stage": stage_name,
                "status": "ERROR",
                "log": f"Stage '{stage_name}' failed: {exc}",
            })
            await _emit({
                "stage": "PIPELINE_ERROR",
                "status": "ERROR",
                "log": f"Pipeline aborted at stage '{stage_name}'",
            })
            # Run error handler
            state["error"] = str(exc)
            state = handle_error(state)
            return state.get("validated_map", {})

    # Pipeline complete
    await _emit({
        "stage": "PIPELINE_COMPLETE",
        "status": "COMPLETED",
        "log": "All stages completed successfully",
        "payload": {
            "obligation_count": len(
                state.get("validated_map", {}).get("obligations", [])
            ),
            "conflicts_found": len(state.get("conflict_results", [])),
            "validated_map": state.get("validated_map", {}),
            "conflict_results": state.get("conflict_results", []),
        },
    })

    result = state.get("validated_map")
    if result is None:
        raise RuntimeError("Pipeline produced no output map.")

    return result


def _safe_payload(state: dict[str, Any], stage_name: str) -> dict[str, Any]:
    """Extract a small, JSON-safe payload summary for a completed stage."""
    try:
        if stage_name == "obligation_extraction":
            raw = state.get("obligations_raw", [])
            return {"count": len(raw)}
        elif stage_name == "action_decomposition":
            dec = state.get("obligations_decomposed", [])
            return {"count": len(dec)}
        elif stage_name == "sme_rule_injection":
            enr = state.get("obligations_enriched", [])
            return {"count": len(enr)}
        elif stage_name == "json_structuring":
            sm = state.get("structured_map", {})
            return {"obligation_count": len(sm.get("obligations", []))}
        elif stage_name == "guardrails_validation":
            vm = state.get("validated_map", {})
            validation = vm.get("validation", {})
            return {"passed": validation.get("passed", False)}
        elif stage_name == "conflict_detector":
            cr = state.get("conflict_results", [])
            return {"conflicts_found": len(cr)}
    except Exception:
        pass
    return {}
