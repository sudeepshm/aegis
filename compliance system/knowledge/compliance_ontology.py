"""
knowledge/compliance_ontology.py
=================================
NetworkX-backed regulatory knowledge graph.

The ontology is the single source of truth for:
  - What regulations exist and their attributes
  - How regulations relate to each other (supersedes, conflicts, implements, depends_on)
  - Historical validity windows (effective_date / expiry_date)

Design
------
  Nodes  → regulatory obligations / circular references
  Edges  → typed relationships between obligations
  Graph  → NetworkX DiGraph (directed — direction encodes semantic meaning)

Edge direction semantics
------------------------
  A ──SUPERSEDES──► B            : A replaces B (A is newer)
  A ──CONFLICTS_WITH──► B        : A and B have contradictory requirements
  A ──IMPLEMENTS──► B            : A is a concrete implementation of abstract B
  A ──DEPENDS_ON──► B            : A cannot be complied with unless B is satisfied first
  A ──RELATED_TO──► B            : A and B share thematic overlap
  A ──EQUIVALENT_TO──► B         : A and B impose equivalent obligations
  A ──REQUIRES_PREREQUISITE──► B : Task A cannot START until task B is CLOSED
                                   Used for DAG execution chains in rollout_sequence.
                                   ServiceNow mapping: B = parent, A = child (On Hold).

Usage
-----
    from knowledge.compliance_ontology import ComplianceOntology
    onto = ComplianceOntology()          # loads seed on init
    node = onto.get_node("rbi_it_framework_2023")
    conflicts = onto.get_conflicts("rbi_it_framework_2023")
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ── Optional imports ──────────────────────────────────────────────────────────
try:
    import networkx as nx
    _NX_AVAILABLE = True
except ImportError:
    _NX_AVAILABLE = False
    logger.warning("networkx not installed — ComplianceOntology will use dict fallback")

try:
    import yaml
    _YAML_AVAILABLE = True
except ImportError:
    _YAML_AVAILABLE = False
    logger.warning("PyYAML not installed — seed data cannot be loaded from regulations.yaml")

# Default seed file path (relative to this module)
_SEED_FILE = Path(__file__).parent / "seeds" / "regulations.yaml"


# =============================================================================
# Enumerations
# =============================================================================

class EdgeType(str, Enum):
    SUPERSEDES            = "SUPERSEDES"
    CONFLICTS_WITH        = "CONFLICTS_WITH"
    IMPLEMENTS            = "IMPLEMENTS"
    DEPENDS_ON            = "DEPENDS_ON"
    RELATED_TO            = "RELATED_TO"
    EQUIVALENT_TO         = "EQUIVALENT_TO"
    REQUIRES_PREREQUISITE = "REQUIRES_PREREQUISITE"   # Phase 9: DAG task dependency


class RegulatorySource(str, Enum):
    RBI     = "RBI"
    SEBI    = "SEBI"
    CERT_IN = "CERT_IN"
    DPDP    = "DPDP"
    BASEL   = "BASEL"
    IRDAI   = "IRDAI"
    PFRDA   = "PFRDA"


class Domain(str, Enum):
    ENCRYPTION      = "encryption"
    CYBER           = "cyber"
    KYC             = "kyc"
    AML             = "aml"
    DATA_PROTECTION = "data_protection"
    CAPITAL         = "capital"
    LIQUIDITY       = "liquidity"
    OUTSOURCING     = "outsourcing"
    FRAUD           = "fraud"
    AUDIT           = "audit"


class PenaltySeverity(str, Enum):
    HIGH   = "HIGH"
    MEDIUM = "MEDIUM"
    LOW    = "LOW"


# =============================================================================
# Data Structures
# =============================================================================

@dataclass
class RegNode:
    """A regulatory obligation / circular node in the ontology graph."""
    id:               str
    reg_id:           str
    source:           str
    title:            str
    effective_date:   date
    expiry_date:      Optional[date]
    mandatory:        bool
    subject:          str
    domain:           str
    summary:          str
    clauses:          list[str] = field(default_factory=list)
    penalty_severity: str = "MEDIUM"
    metadata:         dict[str, Any] = field(default_factory=dict)

    def is_active(self, as_of: Optional[date] = None) -> bool:
        """Return True if this regulation is in force on `as_of` date."""
        check_date = as_of or date.today()
        if check_date < self.effective_date:
            return False
        if self.expiry_date and check_date > self.expiry_date:
            return False
        return True

    def to_dict(self) -> dict[str, Any]:
        return {
            "id":               self.id,
            "reg_id":           self.reg_id,
            "source":           self.source,
            "title":            self.title,
            "effective_date":   self.effective_date.isoformat(),
            "expiry_date":      self.expiry_date.isoformat() if self.expiry_date else None,
            "mandatory":        self.mandatory,
            "subject":          self.subject,
            "domain":           self.domain,
            "summary":          self.summary,
            "clauses":          self.clauses,
            "penalty_severity": self.penalty_severity,
        }


@dataclass
class RegEdge:
    """A typed relationship between two regulatory nodes."""
    source_id:      str
    target_id:      str
    edge_type:      EdgeType
    effective_date: date
    notes:          str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id":      self.source_id,
            "target_id":      self.target_id,
            "edge_type":      self.edge_type.value,
            "effective_date": self.effective_date.isoformat(),
            "notes":          self.notes,
        }


# =============================================================================
# Compliance Ontology
# =============================================================================

class ComplianceOntology:
    """
    NetworkX directed graph of regulatory obligations and their relationships.

    The graph is loaded from a YAML seed file at instantiation, then enriched
    at runtime as new documents are ingested.

    Thread Safety
    -------------
    Reads are safe concurrently.  Writes (add_node, add_edge) should be
    guarded externally if called from multiple async tasks simultaneously.
    In the LangGraph pipeline, the ontology is read-only after startup.
    """

    def __init__(self, seed_path: Optional[Path] = None, auto_load: bool = True):
        self._seed_path = seed_path or _SEED_FILE
        # Primary storage: id → RegNode
        self._nodes: dict[str, RegNode] = {}
        # Edge list (in addition to graph)
        self._edges: list[RegEdge] = []

        # NetworkX graph (if available)
        self._graph = nx.DiGraph() if _NX_AVAILABLE else None

        if auto_load:
            self._load_seed()

    # ── Seed Loading ──────────────────────────────────────────────────────────

    def _load_seed(self) -> None:
        """Load regulatory nodes and edges from the YAML seed file."""
        if not _YAML_AVAILABLE:
            logger.error("PyYAML not available — seed load skipped")
            return
        if not self._seed_path.exists():
            logger.error("Seed file not found: %s", self._seed_path)
            return

        try:
            with self._seed_path.open("r", encoding="utf-8") as fh:
                data = yaml.safe_load(fh)
        except Exception as exc:
            logger.error("Failed to parse seed YAML: %s", exc)
            return

        node_count = 0
        for raw in data.get("nodes", []):
            node = self._parse_node(raw)
            if node:
                self.add_node(node)
                node_count += 1

        edge_count = 0
        for raw in data.get("edges", []):
            edge = self._parse_edge(raw)
            if edge:
                self.add_edge(edge)
                edge_count += 1

        logger.info(
            "ComplianceOntology loaded: %d nodes, %d edges from %s",
            node_count, edge_count, self._seed_path.name,
        )

    def _parse_node(self, raw: dict) -> Optional[RegNode]:
        try:
            eff_str = raw.get("effective_date", "2000-01-01")
            exp_str = raw.get("expiry_date")
            eff_date = date.fromisoformat(str(eff_str))
            exp_date = date.fromisoformat(str(exp_str)) if exp_str else None
            return RegNode(
                id               = raw["id"],
                reg_id           = raw.get("reg_id", raw["id"]),
                source           = raw.get("source", "UNKNOWN"),
                title            = raw.get("title", ""),
                effective_date   = eff_date,
                expiry_date      = exp_date,
                mandatory        = bool(raw.get("mandatory", True)),
                subject          = raw.get("subject", ""),
                domain           = raw.get("domain", ""),
                summary          = raw.get("summary", ""),
                clauses          = raw.get("clauses", []),
                penalty_severity = raw.get("penalty_severity", "MEDIUM"),
            )
        except Exception as exc:
            logger.warning("Failed to parse node %s: %s", raw.get("id"), exc)
            return None

    def _parse_edge(self, raw: dict) -> Optional[RegEdge]:
        try:
            eff_str = raw.get("effective_date", "2000-01-01")
            return RegEdge(
                source_id      = raw["source_id"],
                target_id      = raw["target_id"],
                edge_type      = EdgeType(raw["edge_type"]),
                effective_date = date.fromisoformat(str(eff_str)),
                notes          = raw.get("notes", ""),
            )
        except Exception as exc:
            logger.warning("Failed to parse edge %s→%s: %s",
                           raw.get("source_id"), raw.get("target_id"), exc)
            return None

    # ── Mutation API ─────────────────────────────────────────────────────────

    def add_node(self, node: RegNode) -> None:
        """Add or update a regulatory node in the ontology."""
        self._nodes[node.id] = node
        if self._graph is not None:
            self._graph.add_node(node.id, **{
                "reg_id":           node.reg_id,
                "source":           node.source,
                "title":            node.title,
                "effective_date":   node.effective_date,
                "expiry_date":      node.expiry_date,
                "mandatory":        node.mandatory,
                "domain":           node.domain,
                "penalty_severity": node.penalty_severity,
            })

    def add_edge(self, edge: RegEdge) -> None:
        """Add a typed relationship edge to the ontology."""
        # Only add edge if both nodes exist
        if edge.source_id not in self._nodes or edge.target_id not in self._nodes:
            logger.warning("Edge %s→%s skipped: one or both nodes missing",
                           edge.source_id, edge.target_id)
            return
        self._edges.append(edge)
        if self._graph is not None:
            self._graph.add_edge(
                edge.source_id,
                edge.target_id,
                edge_type      = edge.edge_type.value,
                effective_date = edge.effective_date,
                notes          = edge.notes,
            )

    def add_node_from_ingestion(
        self,
        doc_id: str,
        reg_id: str,
        source: str,
        title: str,
        summary: str,
        effective_date: date,
        domain: str = "cyber",
        clauses: Optional[list[str]] = None,
    ) -> RegNode:
        """
        Called by the ingestion pipeline to register a newly ingested circular.
        Returns the created node.
        """
        node = RegNode(
            id             = doc_id,
            reg_id         = reg_id,
            source         = source,
            title          = title,
            effective_date = effective_date,
            expiry_date    = None,
            mandatory      = True,
            subject        = "Regulated Entities",
            domain         = domain,
            summary        = summary,
            clauses        = clauses or [],
        )
        self.add_node(node)
        logger.info("Ontology enriched with ingested document: %s (%s)", reg_id, doc_id)
        return node

    # ── Query API ─────────────────────────────────────────────────────────────

    def get_node(self, node_id: str) -> Optional[RegNode]:
        """Return a node by its internal ID."""
        return self._nodes.get(node_id)

    def get_node_by_reg_id(self, reg_id: str) -> Optional[RegNode]:
        """Return a node by its official regulatory reference (e.g. 'RBI/2023-24/101')."""
        reg_id_lower = reg_id.lower()
        for node in self._nodes.values():
            if node.reg_id.lower() == reg_id_lower:
                return node
        # Fuzzy: check if reg_id is a substring
        for node in self._nodes.values():
            if reg_id_lower in node.reg_id.lower():
                return node
        return None

    def get_all_nodes(self, active_only: bool = False,
                      as_of: Optional[date] = None) -> list[RegNode]:
        """Return all nodes, optionally filtered to active ones."""
        nodes = list(self._nodes.values())
        if active_only:
            check = as_of or date.today()
            nodes = [n for n in nodes if n.is_active(check)]
        return nodes

    def get_nodes_by_domain(self, domain: str) -> list[RegNode]:
        """Return all nodes for a specific regulatory domain."""
        return [n for n in self._nodes.values() if n.domain == domain]

    def get_nodes_by_source(self, source: str) -> list[RegNode]:
        """Return all nodes for a specific regulatory source (e.g. 'RBI')."""
        return [n for n in self._nodes.values() if n.source == source]

    def get_edges(
        self,
        source_id: Optional[str] = None,
        target_id: Optional[str] = None,
        edge_type: Optional[EdgeType] = None,
    ) -> list[RegEdge]:
        """Filtered edge query."""
        result = self._edges
        if source_id:
            result = [e for e in result if e.source_id == source_id]
        if target_id:
            result = [e for e in result if e.target_id == target_id]
        if edge_type:
            result = [e for e in result if e.edge_type == edge_type]
        return result

    def get_superseded_by(self, node_id: str) -> list[RegNode]:
        """Return nodes that supersede `node_id` (i.e. node_id is the old one)."""
        edges = self.get_edges(target_id=node_id, edge_type=EdgeType.SUPERSEDES)
        return [self._nodes[e.source_id] for e in edges if e.source_id in self._nodes]

    def get_supersedes(self, node_id: str) -> list[RegNode]:
        """Return nodes that `node_id` supersedes (i.e. the old ones)."""
        edges = self.get_edges(source_id=node_id, edge_type=EdgeType.SUPERSEDES)
        return [self._nodes[e.target_id] for e in edges if e.target_id in self._nodes]

    def get_conflicts(self, node_id: str) -> list[tuple[RegNode, str]]:
        """
        Return nodes that conflict with `node_id`.
        Returns list of (conflicting_node, notes).
        Conflict edges are bidirectional semantically, so we check both directions.
        """
        result: list[tuple[RegNode, str]] = []
        # A conflicts with B (outgoing)
        for e in self.get_edges(source_id=node_id, edge_type=EdgeType.CONFLICTS_WITH):
            if e.target_id in self._nodes:
                result.append((self._nodes[e.target_id], e.notes))
        # B conflicts with A (incoming — conflicts are symmetric)
        for e in self.get_edges(target_id=node_id, edge_type=EdgeType.CONFLICTS_WITH):
            if e.source_id in self._nodes:
                result.append((self._nodes[e.source_id], e.notes))
        return result

    def get_dependencies(self, node_id: str) -> list[RegNode]:
        """Return nodes that `node_id` depends on (must be satisfied first)."""
        edges = self.get_edges(source_id=node_id, edge_type=EdgeType.DEPENDS_ON)
        return [self._nodes[e.target_id] for e in edges if e.target_id in self._nodes]

    def get_prerequisites(self, node_id: str) -> list[RegNode]:
        """
        Phase 9 — DAG support.
        Return nodes that must be completed before `node_id` can start.
        (REQUIRES_PREREQUISITE edges: node_id ──REQUIRES_PREREQUISITE──► prerequisite)
        """
        edges = self.get_edges(source_id=node_id, edge_type=EdgeType.REQUIRES_PREREQUISITE)
        return [self._nodes[e.target_id] for e in edges if e.target_id in self._nodes]

    def get_dependents(self, node_id: str) -> list[RegNode]:
        """
        Phase 9 — DAG support.
        Return nodes that are BLOCKED by `node_id` — i.e., they cannot start until
        `node_id` is closed. (Reverse REQUIRES_PREREQUISITE traversal.)
        """
        edges = self.get_edges(target_id=node_id, edge_type=EdgeType.REQUIRES_PREREQUISITE)
        return [self._nodes[e.source_id] for e in edges if e.source_id in self._nodes]

    def get_implementors(self, node_id: str) -> list[RegNode]:
        """Return concrete implementations of an abstract regulatory requirement."""
        edges = self.get_edges(target_id=node_id, edge_type=EdgeType.IMPLEMENTS)
        return [self._nodes[e.source_id] for e in edges if e.source_id in self._nodes]

    # ── Graph Analytics (NetworkX required) ──────────────────────────────────

    def get_shortest_compliance_path(
        self, source_id: str, target_id: str
    ) -> list[str]:
        """
        Return the shortest path from source to target in the graph.
        Useful for: "how does obligation A relate to obligation B?"
        Returns [] if no path exists or NetworkX is unavailable.
        """
        if self._graph is None or source_id not in self._graph or target_id not in self._graph:
            return []
        try:
            return nx.shortest_path(self._graph, source_id, target_id)
        except nx.NetworkXNoPath:
            return []
        except Exception as exc:
            logger.warning("Shortest path computation failed: %s", exc)
            return []

    def get_all_related(self, node_id: str, max_hops: int = 2) -> list[RegNode]:
        """
        Return all nodes reachable within `max_hops` of `node_id`.
        Used by CriticAgent to find related obligations for conflict checking.
        """
        if self._graph is None or node_id not in self._graph:
            return []
        try:
            reachable = nx.single_source_shortest_path_length(
                self._graph, node_id, cutoff=max_hops
            )
            return [
                self._nodes[nid]
                for nid in reachable
                if nid != node_id and nid in self._nodes
            ]
        except Exception as exc:
            logger.warning("Graph traversal failed: %s", exc)
            return []

    def get_regulatory_clusters(self) -> dict[str, list[str]]:
        """
        Return clusters of strongly connected regulatory obligations
        (using Tarjan's algorithm via NetworkX).
        Useful for: identifying circular dependencies or tightly coupled regulation groups.
        """
        if self._graph is None:
            return {}
        try:
            clusters: dict[str, list[str]] = {}
            for i, component in enumerate(nx.weakly_connected_components(self._graph)):
                domains = set(
                    self._nodes[n].domain
                    for n in component
                    if n in self._nodes
                )
                cluster_key = f"cluster_{i}_{'+'.join(sorted(domains))}"
                clusters[cluster_key] = list(component)
            return clusters
        except Exception as exc:
            logger.warning("Cluster computation failed: %s", exc)
            return {}

    def find_nodes_by_keyword(self, keyword: str) -> list[RegNode]:
        """
        Full-text search across node titles, summaries, and clause text.
        Used by DeepReasoningAgent to find relevant regulations for a given obligation.
        """
        kw = keyword.lower()
        results: list[RegNode] = []
        for node in self._nodes.values():
            searchable = (
                f"{node.title} {node.summary} {' '.join(node.clauses)}"
            ).lower()
            if kw in searchable:
                results.append(node)
        return results

    def find_conflicting_standards(
        self, domain: str, requirement_text: str
    ) -> list[dict[str, Any]]:
        """
        Find potential conflicts for a given requirement text within a domain.
        Returns a list of conflict reports with node details and conflict notes.

        Used by CriticAgent.
        """
        domain_nodes = self.get_nodes_by_domain(domain)
        conflicts: list[dict[str, Any]] = []

        # Check each domain node for CONFLICTS_WITH relationships
        for node in domain_nodes:
            for conflicting_node, notes in self.get_conflicts(node.id):
                conflicts.append({
                    "node_a":    node.to_dict(),
                    "node_b":    conflicting_node.to_dict(),
                    "notes":     notes,
                    "relevance": self._compute_text_relevance(
                        requirement_text,
                        f"{node.summary} {' '.join(node.clauses)}",
                    ),
                })

        # Sort by relevance
        conflicts.sort(key=lambda x: x["relevance"], reverse=True)
        return conflicts

    @staticmethod
    def _compute_text_relevance(query: str, text: str) -> float:
        """Simple word-overlap relevance score (0.0–1.0)."""
        query_words = set(query.lower().split())
        text_words  = set(text.lower().split())
        if not query_words:
            return 0.0
        overlap = query_words & text_words
        return len(overlap) / len(query_words)

    # ── Serialisation ─────────────────────────────────────────────────────────

    def to_dict(self) -> dict[str, Any]:
        """Serialise the full ontology (for audit trail / API response)."""
        return {
            "node_count": len(self._nodes),
            "edge_count": len(self._edges),
            "nodes":      [n.to_dict() for n in self._nodes.values()],
            "edges":      [e.to_dict() for e in self._edges],
        }

    def summary(self) -> str:
        sources = {}
        for node in self._nodes.values():
            sources[node.source] = sources.get(node.source, 0) + 1
        source_str = ", ".join(f"{s}:{c}" for s, c in sorted(sources.items()))
        return (
            f"ComplianceOntology: {len(self._nodes)} nodes "
            f"({source_str}), {len(self._edges)} edges"
        )
