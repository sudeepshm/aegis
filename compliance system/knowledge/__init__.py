"""
knowledge/
==========
Domain-specific regulatory intelligence layer.

This package provides structured, deterministic compliance knowledge
independent of any LLM — making it the core IP of the platform.

Modules
-------
  compliance_ontology  – NetworkX-backed regulatory knowledge graph
  policy_graph         – Query layer (supersessions, conflicts, paths)
  rule_compiler        – Regulatory text → CompiledRule objects
  temporal_reasoner    – Date-aware obligation validity
  risk_engine          – Multi-factor risk scoring engine
"""
from knowledge.compliance_ontology import ComplianceOntology, EdgeType, RegNode
from knowledge.policy_graph import PolicyGraph
from knowledge.rule_compiler import RuleCompiler, CompiledRule
from knowledge.temporal_reasoner import TemporalReasoner

__all__ = [
    "ComplianceOntology",
    "EdgeType",
    "RegNode",
    "PolicyGraph",
    "RuleCompiler",
    "CompiledRule",
    "TemporalReasoner",
]
