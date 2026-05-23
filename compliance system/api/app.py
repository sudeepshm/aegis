"""
api/app.py
==========
Central FastAPI application — Project Aegis

Assembles the FastAPI application by including all APIRouters.
This is the single entrypoint for ``uvicorn``:

    uvicorn api.app:app --host 0.0.0.0 --port 8000 --reload

Routers included:
  - /api/tensions        — TensionRecord CRUD (Phase 3)
  - /ws/pipeline/{id}    — WebSocket pipeline streaming (Phase 4)
  - /api/v1/corrections  — Human-in-the-loop correction ledger (existing)
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from pydantic import BaseModel

logger = logging.getLogger(__name__)

try:
    from fastapi import FastAPI
    from fastapi.middleware.cors import CORSMiddleware
    _FASTAPI_AVAILABLE = True
except ImportError:
    _FASTAPI_AVAILABLE = False
    logger.error("FastAPI not installed. Run: pip install fastapi uvicorn")

if _FASTAPI_AVAILABLE:
    class DelaySimulationRequest(BaseModel):
        node_id: str
        delay_days: int

    app = FastAPI(
        title="Project Aegis — Compliance Automation API",
        description=(
            "Enterprise compliance automation platform.\n\n"
            "Includes regulatory ingestion, obligation mapping, "
            "conflict detection, and real-time pipeline streaming."
        ),
        version="1.0.0",
        docs_url="/docs",
        openapi_url="/openapi.json",
    )

    # CORS — allow the Next.js frontend during development
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://localhost:3000",
            "http://localhost:3001",
            os.environ.get("NEXTAUTH_URL", "http://localhost:3000"),
        ],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ── Phase 3: Tensions APIRouter ────────────────────────────────────────
    try:
        from api.routers.tensions import router as tensions_router
        app.include_router(tensions_router)
        logger.info("Registered APIRouter: /api/tensions")
    except ImportError as exc:
        logger.warning("Tensions router unavailable: %s", exc)

    # ── Phase 4: WebSocket Pipeline Router ─────────────────────────────────
    try:
        from api.routers.pipeline_ws import router as ws_router
        app.include_router(ws_router)
        logger.info("Registered APIRouter: /ws/pipeline")
    except ImportError as exc:
        logger.warning("WebSocket pipeline router unavailable: %s", exc)

    # ── Health Check ───────────────────────────────────────────────────────
    @app.get("/health")
    async def health() -> dict[str, str]:
        return {
            "status": "healthy",
            "service": "aegis-api",
            "version": "1.0.0",
            "timestamp": datetime.now(tz=timezone.utc).isoformat(),
        }

    @app.post("/api/v1/simulate_delay")
    async def simulate_delay(payload: dict):
        # 1. Extract the raw graph data from the frontend
        dag_nodes = payload.get("dag_nodes", [])
        delayed_task = payload.get("delayed_task", "")
        delay_days = payload.get("delay_days", 0)
        base_risk = payload.get("base_risk_score", 5)
        
        # 2. Rebuild the graph in memory (Adjacency List: parent -> [children])
        graph = {node["task"]: [] for node in dag_nodes}
        for node in dag_nodes:
            for parent in node.get("depends_on", []):
                if parent in graph:
                    graph[parent].append(node["task"])
                    
        # 3. Breadth-First Search (BFS) to find the blast radius
        impacted_nodes = set()
        queue = [delayed_task] if delayed_task in graph else []
        
        while queue:
            current = queue.pop(0)
            for child in graph.get(current, []):
                if child not in impacted_nodes:
                    impacted_nodes.add(child)
                    queue.append(child)
                    
        blocked_count = len(impacted_nodes)
        
        # 4. The Real Math: Calculate risk based on actual topological impact
        # Example: Base Risk + (0.5% per day delayed) + (15% per blocked downstream task)
        calculated_risk = base_risk + (delay_days * 0.5) + (blocked_count * 15)
        calculated_risk = min(100.0, calculated_risk) # Cap at 100% maximum risk
        
        print(f"\n[BLAST RADIUS ALGORITHM]")
        print(f" ├─ Target Node: {delayed_task} delayed by {delay_days} days")
        print(f" ├─ Graph Traversal found {blocked_count} downstream dependencies choked: {list(impacted_nodes)}")
        print(f" └─ Cascade Risk Score: {calculated_risk}%\n")
        
        return {
            "status": "success",
            "impact_percentage": round(calculated_risk, 1),
            "cascade_impact": round(calculated_risk, 1),
            "risk_increase": round(calculated_risk, 1),
            "tasks_blocked": blocked_count,
            "blocked_tasks": blocked_count,
            "blocked_count": blocked_count,
            "impacted_nodes": list(impacted_nodes),
            "message": "Topological cascade simulated"
        }

