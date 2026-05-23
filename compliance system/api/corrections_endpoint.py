"""
api/corrections_endpoint.py
============================
Phase 9 — Feature 5: Human-in-the-Loop Correction Ledger API.

POST /api/v1/corrections       — Log a CCO correction
GET  /api/v1/corrections       — Query recent corrections (exact-first, domain fallback)
POST /api/v1/simulate_delay    — On-demand blast radius simulation

Authentication: Bearer token (Azure AD) via X-API-Key header (dev mode).
"""
from __future__ import annotations

import logging
import os
from datetime import date, datetime, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ── FastAPI imports ────────────────────────────────────────────────────────────
try:
    from fastapi import FastAPI, HTTPException, Header, Query
    from fastapi.responses import JSONResponse
    from pydantic import BaseModel, Field
    _FASTAPI_AVAILABLE = True
except ImportError:
    _FASTAPI_AVAILABLE = False
    logger.warning(
        "fastapi not installed — corrections_endpoint not available. "
        "Install with: pip install fastapi uvicorn"
    )


# ── Internal imports ───────────────────────────────────────────────────────────
try:
    from audit.logger import AuditLogger, AuditLoggerConfig, CorrectionLedgerClient
    _AUDIT_AVAILABLE = True
except ImportError:
    _AUDIT_AVAILABLE = False

try:
    from knowledge.simulator import BlastRadiusSimulator
    _SIM_AVAILABLE = True
except ImportError:
    _SIM_AVAILABLE = False


# =============================================================================
# Request / Response Models
# =============================================================================

if _FASTAPI_AVAILABLE:
    class CorrectionRequest(BaseModel):
        """POST /api/v1/corrections — CCO correction payload."""
        chain_id:        str = Field(..., description="Audit chain ID the correction belongs to")
        corrected_by:    str = Field(..., description="AAD object ID or email of the CCO")
        stage:           str = Field(..., description="Pipeline stage: routing | risk_scoring | assignment")
        regulation_ref:  str = Field(..., description="Regulation ID, e.g. rbi_kyc_master_2016")
        domain:          str = Field(..., description="Regulatory domain, e.g. kyc | aml | cyber")
        original_value:  Any = Field(..., description="Original system output")
        corrected_value: Any = Field(..., description="Corrected value from CCO")
        comment:         str = Field("", description="Free-text rationale")

    class SimulateDelayRequest(BaseModel):
        """POST /api/v1/simulate_delay — blast radius simulation payload."""
        dag_nodes:       list[dict] = Field(..., description="Validated DAG from ReasoningResult.dag_nodes")
        delayed_task:    str = Field(..., description="Task name being hypothetically delayed")
        delay_days:      int = Field(..., gt=0, description="Number of days delayed")
        regulation_deadlines: Optional[dict[str, str]] = Field(
            default=None,
            description="Task name -> ISO date string (YYYY-MM-DD) of regulatory deadlines"
        )
        task_duration_days: int = Field(default=5, description="Assumed task duration in days")
        base_risk_score:    float = Field(default=5.0, description="Current risk score (0-10)")


# =============================================================================
# Application Factory
# =============================================================================

def create_corrections_app(
    audit_logger: Optional["AuditLogger"] = None,
    dev_api_key: str = "",
) -> "FastAPI":
    """
    Create and return the FastAPI corrections application.

    Args:
        audit_logger: AuditLogger instance (creates in-memory one if not provided)
        dev_api_key:  Simple API key for dev auth (pass empty string to disable)

    Returns the FastAPI app with registered routes.
    """
    if not _FASTAPI_AVAILABLE:
        raise ImportError("fastapi is required. Install with: pip install fastapi uvicorn")

    app = FastAPI(
        title="Compliance Correction Ledger API",
        description=(
            "Phase 9 — Enterprise Intelligence Upgrade.\n\n"
            "Human-in-the-Loop correction ledger and blast radius simulation API."
        ),
        version="9.0.0",
        docs_url="/docs",
        openapi_url="/openapi.json",
    )

    # Build or reuse audit logger
    _logger = audit_logger or AuditLogger(AuditLoggerConfig())
    _ledger_client = CorrectionLedgerClient(_logger._worm) if _AUDIT_AVAILABLE else None
    _simulator = BlastRadiusSimulator() if _SIM_AVAILABLE else None

    def _auth(x_api_key: str = Header(default="")) -> bool:
        """Simple API key check for dev environments."""
        if dev_api_key and x_api_key != dev_api_key:
            raise HTTPException(status_code=401, detail="Invalid API key")
        return True

    # ── Routes ────────────────────────────────────────────────────────────────

    @app.post("/api/v1/corrections", status_code=201)
    async def log_correction(
        request: CorrectionRequest,
        x_api_key: str = Header(default=""),
    ) -> JSONResponse:
        """
        POST /api/v1/corrections

        Log a Chief Compliance Officer correction to the immutable WORM audit chain.
        The correction is stored as a HUMAN_CORRECTION node with full hash-chain integrity.

        Response includes the correction_id for reference in future few-shot contexts.
        """
        _auth(x_api_key)

        if not _AUDIT_AVAILABLE:
            raise HTTPException(status_code=503, detail="Audit logger not available")

        # Ensure chain exists — create if not
        try:
            chain = _logger.get_chain(request.chain_id)
            if chain is None:
                logger.info("Chain %s not found — creating new chain", request.chain_id)
                request.chain_id = _logger.new_chain()
        except Exception:
            request.chain_id = _logger.new_chain()

        try:
            correction_id = await _logger.log_human_correction(
                chain_id        = request.chain_id,
                corrected_by    = request.corrected_by,
                stage           = request.stage,
                original_value  = request.original_value,
                corrected_value = request.corrected_value,
                regulation_ref  = request.regulation_ref,
                domain          = request.domain,
                comment         = request.comment,
            )
        except Exception as exc:
            logger.error("Failed to log correction: %s", exc)
            raise HTTPException(status_code=500, detail=f"Failed to log correction: {exc}")

        return JSONResponse(
            status_code=201,
            content={
                "status":        "LOGGED",
                "correction_id": correction_id,
                "chain_id":      request.chain_id,
                "message":       (
                    f"Correction logged to audit chain {request.chain_id}. "
                    f"This will be injected as few-shot context in future {request.stage} decisions."
                ),
                "timestamp":     datetime.now(tz=timezone.utc).isoformat(),
            }
        )

    @app.get("/api/v1/corrections")
    async def get_corrections(
        domain:         str = Query(..., description="Regulatory domain"),
        regulation_ref: str = Query(default="", description="Optional: specific regulation ID"),
        limit:          int = Query(default=5, le=20, description="Max results"),
        x_api_key:      str = Header(default=""),
    ) -> JSONResponse:
        """
        GET /api/v1/corrections

        Query past human corrections using Option C scope:
          1. Exact match by regulation_ref + domain
          2. Domain fallback if no exact matches

        Used by RiskScoringEngine and Router to inject few-shot context.
        """
        _auth(x_api_key)

        if not _ledger_client:
            raise HTTPException(status_code=503, detail="Correction ledger not available")

        try:
            corrections = await _ledger_client.get_recent_corrections(
                domain=domain, regulation_ref=regulation_ref, limit=limit
            )
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Query failed: {exc}")

        return JSONResponse(content={
            "domain":          domain,
            "regulation_ref":  regulation_ref,
            "count":           len(corrections),
            "corrections":     corrections,
        })

    @app.post("/api/v1/simulate_delay")
    async def simulate_delay(
        request: SimulateDelayRequest,
        x_api_key: str = Header(default=""),
    ) -> JSONResponse:
        """
        POST /api/v1/simulate_delay

        On-demand blast radius simulation (Feature 3).

        Given a validated compliance task DAG and a hypothetical delay scenario,
        returns the full downstream cascade impact, regulatory deadline breaches,
        risk score delta, and a CCO-ready narrative.

        This endpoint runs OUTSIDE the main ingestion pipeline to avoid
        latency bottlenecks (Approved Architecture: Option B).
        """
        _auth(x_api_key)

        if not _simulator:
            raise HTTPException(status_code=503, detail="BlastRadiusSimulator not available")

        # Parse date strings to date objects
        deadlines: dict[str, date] = {}
        if request.regulation_deadlines:
            for task_name, date_str in request.regulation_deadlines.items():
                try:
                    deadlines[task_name] = date.fromisoformat(date_str)
                except ValueError:
                    raise HTTPException(
                        status_code=422,
                        detail=f"Invalid date format for task '{task_name}': '{date_str}'. Use YYYY-MM-DD."
                    )

        try:
            report = _simulator.simulate_delay(
                dag                   = request.dag_nodes,
                delayed_task          = request.delayed_task,
                delay_days            = request.delay_days,
                regulation_deadlines  = deadlines or None,
                task_duration_days    = request.task_duration_days,
                base_risk_score       = request.base_risk_score,
            )
        except Exception as exc:
            logger.error("Simulation failed: %s", exc)
            raise HTTPException(status_code=500, detail=f"Simulation failed: {exc}")

        return JSONResponse(content=report.to_dict())

    @app.get("/health")
    async def health() -> dict[str, str]:
        """Health check endpoint."""
        return {
            "status":          "healthy",
            "service":         "compliance-corrections-api",
            "version":         "9.0.0",
            "audit_available": str(_AUDIT_AVAILABLE),
            "sim_available":   str(_SIM_AVAILABLE),
            "timestamp":       datetime.now(tz=timezone.utc).isoformat(),
        }

    return app


# =============================================================================
# Standalone Entry Point (for uvicorn)
# =============================================================================

if _FASTAPI_AVAILABLE:
    # Default app instance for: uvicorn api.corrections_endpoint:app --reload
    app = create_corrections_app(
        dev_api_key=os.environ.get("CORRECTIONS_API_KEY", "")
    )
