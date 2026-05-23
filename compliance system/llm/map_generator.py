"""
llm/map_generator.py

A 5-stage LangGraph reasoning pipeline for processing semantic chunks into
structured obligation maps with risk categorization and guardrail validation.

Pipeline Stages:
    1. Obligation Extraction Agent     – Extract enforceable directives
    2. Action Decomposition Agent      – Convert obligations → operational actions
    3. SME Rule Injection Layer        – Apply business logic (routing, risk)
    4. JSON Structuring Agent          – Convert to predefined JSON schema
    5. Guardrails & Validation Layer   – Confidence check (threshold: 0.75)
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import datetime, timedelta
from typing import Any, TypedDict

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from langgraph.graph import END, StateGraph

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s – %(message)s",
)
logger = logging.getLogger("map_generator")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
CONFIDENCE_THRESHOLD = 0.75
HIGH_RISK_DAYS = 30      # <= 30 days  → High
MEDIUM_RISK_DAYS = 60    # 31-60 days  → Medium
                         # > 60 days   → Low

# ---------------------------------------------------------------------------
# Output JSON Schema (canonical)
# ---------------------------------------------------------------------------
SCHEMA_DESCRIPTION = """
{
  "obligation_map_id": "<uuid>",
  "source_chunk_id":   "<string>",
  "processed_at":      "<ISO-8601 timestamp>",
  "obligations": [
    {
      "obligation_id":   "<uuid>",
      "raw_text":        "<original extracted text>",
      "directive_type":  "MUST | SHOULD | SHALL | MUST NOT | MAY",
      "actions": [
        {
          "action_id":     "<uuid>",
          "description":   "<operational action description>",
          "owner":         "<responsible party or role>",
          "deadline_days": <integer | null>,
          "risk_level":    "High | Medium | Low",
          "routing_tag":   "<SME-assigned routing tag>"
        }
      ],
      "confidence_score": <float 0.0–1.0>,
      "source_grounded":  <boolean>
    }
  ],
  "validation": {
    "passed":          <boolean>,
    "rejection_reason": "<string | null>"
  }
}
"""

# ---------------------------------------------------------------------------
# State Definition
# ---------------------------------------------------------------------------

class PipelineState(TypedDict, total=False):
    # Input
    chunk_text: str
    chunk_id: str

    # Stage outputs (accumulated)
    obligations_raw: list[dict[str, Any]]        # Stage 1
    obligations_decomposed: list[dict[str, Any]] # Stage 2
    obligations_enriched: list[dict[str, Any]]   # Stage 3
    structured_map: dict[str, Any]               # Stage 4
    validated_map: dict[str, Any]                # Stage 5

    # Meta
    error: str | None


# ---------------------------------------------------------------------------
# LLM Factory
# ---------------------------------------------------------------------------

def _build_llm(model: str = "gpt-4o", temperature: float = 0.0) -> ChatOpenAI:
    """Return a ChatOpenAI instance. Override model via environment if needed."""
    return ChatOpenAI(model=model, temperature=temperature)


# ---------------------------------------------------------------------------
# Helper utilities
# ---------------------------------------------------------------------------

def _parse_json_response(raw: str) -> Any:
    """
    Robustly parse JSON from an LLM response, stripping markdown fences
    and leading/trailing whitespace.
    """
    cleaned = re.sub(r"```(?:json)?", "", raw).strip().strip("`").strip()
    return json.loads(cleaned)


def _risk_from_days(deadline_days: int | None) -> str:
    """Categorise risk level based on deadline in days."""
    if deadline_days is None:
        return "Low"
    if deadline_days <= HIGH_RISK_DAYS:
        return "High"
    if deadline_days <= MEDIUM_RISK_DAYS:
        return "Medium"
    return "Low"


def _new_id() -> str:
    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# Stage 1 – Obligation Extraction Agent
# ---------------------------------------------------------------------------

def stage_obligation_extraction(state: PipelineState) -> PipelineState:
    """
    Extract all enforceable directives from the semantic chunk.
    Returns a list of raw obligation dicts with 'raw_text' and 'directive_type'.
    """
    logger.info("Stage 1 | Obligation Extraction Agent – start")
    llm = _build_llm()

    system = SystemMessage(content=(
        "You are an expert regulatory obligation extraction agent. "
        "Identify ALL enforceable directives in the provided text. "
        "A directive uses language such as: MUST, SHALL, SHOULD, MUST NOT, MAY, REQUIRED, etc. "
        "Return ONLY a JSON array – no preamble, no markdown fences – where each element has:\n"
        "  raw_text       : the verbatim sentence or clause\n"
        "  directive_type : one of MUST | SHOULD | SHALL | MUST NOT | MAY\n"
        "  confidence_score : float 0.0–1.0 indicating extraction certainty\n"
        "  source_grounded  : true if text is directly quoted from the source chunk\n"
        "Example: [{\"raw_text\": \"...\", \"directive_type\": \"MUST\", "
        "\"confidence_score\": 0.92, \"source_grounded\": true}]"
    ))
    human = HumanMessage(content=f"SEMANTIC CHUNK:\n\n{state['chunk_text']}")

    response = llm.invoke([system, human])
    obligations_raw: list[dict] = _parse_json_response(response.content)

    # Attach unique IDs
    for ob in obligations_raw:
        ob["obligation_id"] = _new_id()

    logger.info("Stage 1 | Extracted %d obligation(s)", len(obligations_raw))
    return {**state, "obligations_raw": obligations_raw}


# ---------------------------------------------------------------------------
# Stage 2 – Action Decomposition Agent
# ---------------------------------------------------------------------------

def stage_action_decomposition(state: PipelineState) -> PipelineState:
    """
    Convert each obligation into one or more concrete operational actions
    with owner, deadline estimate, and action description.
    """
    logger.info("Stage 2 | Action Decomposition Agent – start")
    llm = _build_llm()

    system = SystemMessage(content=(
        "You are an expert process analyst. "
        "For each obligation provided, decompose it into concrete operational actions. "
        "Return ONLY a JSON array – no preamble, no markdown – preserving the "
        "'obligation_id', 'raw_text', 'directive_type', 'confidence_score', "
        "'source_grounded' fields and adding an 'actions' array. "
        "Each action must have:\n"
        "  action_id    : unique string id\n"
        "  description  : specific operational step\n"
        "  owner        : responsible role or team (e.g., 'Compliance Team', 'IT Security')\n"
        "  deadline_days: integer number of days from today by which this must be done, or null\n"
        "Keep deadline_days null when the source text gives no deadline hint."
    ))
    human = HumanMessage(content=json.dumps(state["obligations_raw"], indent=2))

    response = llm.invoke([system, human])
    obligations_decomposed: list[dict] = _parse_json_response(response.content)

    # Ensure action_ids are proper UUIDs
    for ob in obligations_decomposed:
        for action in ob.get("actions", []):
            if not action.get("action_id"):
                action["action_id"] = _new_id()

    logger.info("Stage 2 | Decomposed %d obligation(s)", len(obligations_decomposed))
    return {**state, "obligations_decomposed": obligations_decomposed}


# ---------------------------------------------------------------------------
# Stage 3 – SME Rule Injection Layer
# ---------------------------------------------------------------------------

def stage_sme_rule_injection(state: PipelineState) -> PipelineState:
    """
    Apply deterministic business logic:
      - Compute risk_level from deadline_days.
      - Assign routing_tag based on directive_type and owner heuristics.
    This layer is intentionally rule-based (not LLM) for auditability.
    """
    logger.info("Stage 3 | SME Rule Injection Layer – start")

    ROUTING_RULES: dict[str, str] = {
        "MUST NOT": "LEGAL_REVIEW",
        "SHALL":    "COMPLIANCE_QUEUE",
        "MUST":     "COMPLIANCE_QUEUE",
        "SHOULD":   "OPERATIONAL_REVIEW",
        "MAY":      "ADVISORY_TRACK",
    }

    enriched: list[dict] = []
    for ob in state["obligations_decomposed"]:
        directive = ob.get("directive_type", "MAY")
        base_routing = ROUTING_RULES.get(directive, "GENERAL_QUEUE")

        actions_enriched = []
        for action in ob.get("actions", []):
            deadline_days = action.get("deadline_days")
            risk_level = _risk_from_days(deadline_days)

            # Refine routing if High risk
            routing_tag = base_routing
            if risk_level == "High":
                routing_tag = f"URGENT_{base_routing}"

            actions_enriched.append({
                **action,
                "risk_level":  risk_level,
                "routing_tag": routing_tag,
            })

        enriched.append({**ob, "actions": actions_enriched})

    logger.info("Stage 3 | SME rules applied to %d obligation(s)", len(enriched))
    return {**state, "obligations_enriched": enriched}


# ---------------------------------------------------------------------------
# Stage 4 – JSON Structuring Agent
# ---------------------------------------------------------------------------

def stage_json_structuring(state: PipelineState) -> PipelineState:
    """
    Convert enriched obligations into the canonical obligation_map JSON schema.
    An LLM is used to perform the final mapping and fill any missing fields.
    """
    logger.info("Stage 4 | JSON Structuring Agent – start")
    llm = _build_llm()

    system = SystemMessage(content=(
        "You are a JSON schema enforcement agent. "
        "Your ONLY job is to produce a valid JSON object conforming exactly to the schema below. "
        "Do NOT include any text outside the JSON. Do NOT use markdown fences.\n\n"
        f"TARGET SCHEMA:\n{SCHEMA_DESCRIPTION}\n\n"
        "Rules:\n"
        "- Use the provided obligation data as-is; do not invent new facts.\n"
        "- Generate fresh UUIDs for obligation_map_id if not present.\n"
        "- set 'processed_at' to current UTC ISO-8601 timestamp.\n"
        "- Preserve all confidence_score and source_grounded values exactly.\n"
        "- The validation block must be: {\"passed\": true, \"rejection_reason\": null} at this stage."
    ))

    payload = {
        "source_chunk_id": state.get("chunk_id", _new_id()),
        "obligations":     state["obligations_enriched"],
    }
    human = HumanMessage(content=json.dumps(payload, indent=2))

    response = llm.invoke([system, human])
    structured_map: dict = _parse_json_response(response.content)

    # Ensure mandatory top-level fields exist
    structured_map.setdefault("obligation_map_id", _new_id())
    structured_map.setdefault("source_chunk_id", state.get("chunk_id", "unknown"))
    structured_map.setdefault("processed_at", datetime.utcnow().isoformat() + "Z")
    structured_map.setdefault("validation", {"passed": True, "rejection_reason": None})

    logger.info("Stage 4 | Structured map built (map_id=%s)", structured_map["obligation_map_id"])
    return {**state, "structured_map": structured_map}


# ---------------------------------------------------------------------------
# Stage 5 – Guardrails & Validation Layer
# ---------------------------------------------------------------------------

def stage_guardrails_validation(state: PipelineState) -> PipelineState:
    """
    Validate the structured map against quality guardrails:
      1. Every obligation must have confidence_score >= CONFIDENCE_THRESHOLD.
      2. Every obligation must be source_grounded == True.
      3. The map must have at least one obligation.
    Marks validation.passed = False and populates rejection_reason if any check fails.
    """
    logger.info("Stage 5 | Guardrails & Validation Layer – start")
    structured_map = state["structured_map"]
    obligations = structured_map.get("obligations", [])

    rejection_reasons: list[str] = []

    if not obligations:
        rejection_reasons.append("No obligations extracted from chunk.")

    for ob in obligations:
        ob_id = ob.get("obligation_id", "unknown")
        score = ob.get("confidence_score", 0.0)
        grounded = ob.get("source_grounded", False)

        if score < CONFIDENCE_THRESHOLD:
            rejection_reasons.append(
                f"Obligation {ob_id}: confidence_score {score:.2f} "
                f"below threshold {CONFIDENCE_THRESHOLD}."
            )
        if not grounded:
            rejection_reasons.append(
                f"Obligation {ob_id}: source_grounded is False – "
                "obligation cannot be traced to source chunk."
            )

    if rejection_reasons:
        structured_map["validation"] = {
            "passed": False,
            "rejection_reason": " | ".join(rejection_reasons),
        }
        logger.warning("Stage 5 | Validation FAILED: %s", structured_map["validation"]["rejection_reason"])
    else:
        structured_map["validation"] = {"passed": True, "rejection_reason": None}
        logger.info("Stage 5 | Validation PASSED")

    return {**state, "validated_map": structured_map}


# ---------------------------------------------------------------------------
# Error handler
# ---------------------------------------------------------------------------

def handle_error(state: PipelineState) -> PipelineState:
    """Catch-all node that marks the map as failed."""
    logger.error("Pipeline error: %s", state.get("error"))
    failed_map = {
        "obligation_map_id": _new_id(),
        "source_chunk_id":   state.get("chunk_id", "unknown"),
        "processed_at":      datetime.utcnow().isoformat() + "Z",
        "obligations":       [],
        "validation": {
            "passed": False,
            "rejection_reason": f"Pipeline error: {state.get('error', 'Unknown error')}",
        },
    }
    return {**state, "validated_map": failed_map}


# ---------------------------------------------------------------------------
# Graph Assembly
# ---------------------------------------------------------------------------

def _build_graph() -> Any:
    """Construct and compile the LangGraph state machine."""
    graph = StateGraph(PipelineState)

    # Register nodes
    graph.add_node("obligation_extraction", stage_obligation_extraction)
    graph.add_node("action_decomposition",  stage_action_decomposition)
    graph.add_node("sme_rule_injection",    stage_sme_rule_injection)
    graph.add_node("json_structuring",      stage_json_structuring)
    graph.add_node("guardrails_validation", stage_guardrails_validation)
    graph.add_node("error_handler",         handle_error)

    # Linear pipeline edges
    graph.set_entry_point("obligation_extraction")
    graph.add_edge("obligation_extraction", "action_decomposition")
    graph.add_edge("action_decomposition",  "sme_rule_injection")
    graph.add_edge("sme_rule_injection",    "json_structuring")
    graph.add_edge("json_structuring",      "guardrails_validation")
    graph.add_edge("guardrails_validation", END)
    graph.add_edge("error_handler",         END)

    return graph.compile()


# Singleton compiled graph
_PIPELINE = _build_graph()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def process_chunk(chunk_text: str, chunk_id: str | None = None) -> dict[str, Any]:
    """
    Process a single semantic chunk through the 5-stage pipeline.

    Args:
        chunk_text: Raw text of the semantic chunk to be processed.
        chunk_id:   Optional identifier for the chunk (auto-generated if omitted).

    Returns:
        The validated obligation map as a Python dict conforming to SCHEMA_DESCRIPTION.
        The 'validation' block indicates whether the map passed all guardrails.

    Raises:
        RuntimeError: If the pipeline encounters an unrecoverable error.
    """
    if not chunk_text or not chunk_text.strip():
        raise ValueError("chunk_text must be a non-empty string.")

    initial_state: PipelineState = {
        "chunk_text": chunk_text,
        "chunk_id":   chunk_id or _new_id(),
        "error":      None,
    }

    logger.info("Processing chunk_id=%s (%d chars)", initial_state["chunk_id"], len(chunk_text))

    try:
        final_state = _PIPELINE.invoke(initial_state)
    except Exception as exc:
        logger.exception("Unrecoverable pipeline error")
        error_state = {**initial_state, "error": str(exc)}
        final_state = handle_error(error_state)

    result = final_state.get("validated_map")
    if result is None:
        raise RuntimeError("Pipeline produced no output map.")

    return result


def process_chunks(chunks: list[dict[str, str]]) -> list[dict[str, Any]]:
    """
    Batch-process multiple semantic chunks.

    Args:
        chunks: List of dicts with keys 'text' (required) and 'id' (optional).

    Returns:
        List of validated obligation maps, one per chunk.
    """
    results = []
    for item in chunks:
        text = item.get("text", "")
        cid  = item.get("id")
        results.append(process_chunk(text, cid))
    return results


# ---------------------------------------------------------------------------
# CLI entry point (for quick testing)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    SAMPLE_CHUNK = """
    All data processors MUST implement encryption at rest using AES-256 within 15 days
    of this agreement. Vendors SHALL conduct quarterly security audits and provide reports
    to the Compliance Team within 45 days of each quarter end. Personal data MUST NOT be
    transferred to third parties without explicit written consent. The IT Security team
    SHOULD review all access logs monthly. Organisations MAY adopt alternative encryption
    standards provided they meet equivalent security levels, subject to CISO approval
    within 90 days.
    """

    result = process_chunk(SAMPLE_CHUNK, chunk_id="sample-chunk-001")
    print(json.dumps(result, indent=2))
    sys.exit(0 if result["validation"]["passed"] else 1)