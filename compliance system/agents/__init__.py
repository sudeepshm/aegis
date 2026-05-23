"""
agents/
-------
Multi-agent collaboration layer for deep compliance reasoning.

Agents
------
  PlannerAgent     – Orchestrates sub-agents; resolves disagreements
  VerifierAgent    – Detects linguistic ambiguity in obligations
  CriticAgent      – Detects policy conflicts and regulatory precedence
  EvidenceAgent    – Infers required evidence for each obligation
  DeepReasoningAgent – Chain-of-thought reasoning using Gemini API

Agent Message Protocol
----------------------
All inter-agent communication uses the typed AgentMessage dataclass
to ensure every step is auditable.
"""
from agents.planner_agent import PlannerAgent, AgentMessage, MultiAgentResult
from agents.verifier_agent import VerifierAgent, AmbiguityReport
from agents.critic_agent import CriticAgent, CritiqueReport
from agents.evidence_agent import EvidenceAgent, EvidenceManifest
from agents.reasoning_agent import DeepReasoningAgent, ReasoningResult

__all__ = [
    "PlannerAgent",
    "AgentMessage",
    "MultiAgentResult",
    "VerifierAgent",
    "AmbiguityReport",
    "CriticAgent",
    "CritiqueReport",
    "EvidenceAgent",
    "EvidenceManifest",
    "DeepReasoningAgent",
    "ReasoningResult",
]
