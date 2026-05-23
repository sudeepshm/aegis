"""
knowledge/dag_validator.py
===========================
Phase 9 — Feature 1: DAG Execution Chain Validator.

Validates a rollout_sequence represented as a list of task dicts:
  [
    {"task": "Enable MFA",     "depends_on": ["Upgrade IAM to v2", "Configure Azure AD"]},
    {"task": "Upgrade IAM to v2",     "depends_on": []},
    {"task": "Configure Azure AD",    "depends_on": []},
  ]

Provides:
  validate()           → (is_valid, list[error_str])
  topological_order()  → ordered list of task names (prerequisites first)
  detect_cycles()      → list of cycle paths [[A, B, C, A]]
  critical_path()      → longest dependency chain (the bottleneck path)

Uses NetworkX DiGraph for graph operations.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# ── Optional NetworkX import ───────────────────────────────────────────────────
try:
    import networkx as nx
    _NX_AVAILABLE = True
except ImportError:
    _NX_AVAILABLE = False
    logger.warning("networkx not available — DAGValidator will use fallback DFS implementation")


# =============================================================================
# Data Structures
# =============================================================================

@dataclass
class DAGValidationResult:
    """Result of a DAG validation pass."""
    is_valid:         bool = False
    errors:           list[str] = field(default_factory=list)
    warnings:         list[str] = field(default_factory=list)
    node_count:       int = 0
    edge_count:       int = 0
    has_cycles:       bool = False
    cycles:           list[list[str]] = field(default_factory=list)
    topological_order: list[str] = field(default_factory=list)
    critical_path:    list[str] = field(default_factory=list)
    isolated_tasks:   list[str] = field(default_factory=list)   # tasks with no dependencies

    def to_dict(self) -> dict[str, Any]:
        return {
            "is_valid":          self.is_valid,
            "errors":            self.errors,
            "warnings":          self.warnings,
            "node_count":        self.node_count,
            "edge_count":        self.edge_count,
            "has_cycles":        self.has_cycles,
            "cycles":            self.cycles,
            "topological_order": self.topological_order,
            "critical_path":     self.critical_path,
            "isolated_tasks":    self.isolated_tasks,
        }


# =============================================================================
# DAG Validator
# =============================================================================

class DAGValidator:
    """
    Validates and analyses compliance task DAGs from DeepReasoningAgent.

    The input format mirrors the rollout_sequence produced by the reasoning agent:
      [{"task": "...", "depends_on": ["...", "..."]}]

    The validator ensures:
      1. No missing dependency references (dangling edges)
      2. No circular dependencies (would deadlock ServiceNow task queue)
      3. Topological ordering is possible
      4. Critical path is identified for SLA planning

    Usage::
        v = DAGValidator()
        result = v.validate(rollout_sequence)
        if result.is_valid:
            ordered = result.topological_order
            bottleneck = result.critical_path
    """

    def validate(self, dag_nodes: list[dict]) -> DAGValidationResult:
        """
        Full validation pass on a DAG definition.

        Returns a DAGValidationResult with is_valid=True only if:
          - All dependency references resolve to known task names
          - No circular dependencies exist
        """
        result = DAGValidationResult(
            node_count = len(dag_nodes),
            edge_count = sum(len(n.get("depends_on", [])) for n in dag_nodes),
        )

        if not dag_nodes:
            result.is_valid = True
            result.warnings.append("Empty DAG — no tasks defined.")
            return result

        # Build name → node index map
        task_names = {n["task"] for n in dag_nodes if "task" in n}

        # Check for nodes missing the 'task' key
        for i, node in enumerate(dag_nodes):
            if "task" not in node:
                result.errors.append(f"Node[{i}] is missing required 'task' key.")

        # Check for dangling dependency references
        for node in dag_nodes:
            for dep in node.get("depends_on", []):
                if dep not in task_names:
                    result.errors.append(
                        f"Task '{node.get('task')}' depends on '{dep}' which is not defined in the DAG."
                    )

        if result.errors:
            result.is_valid = False
            return result

        # Build graph and run cycle/path analysis
        if _NX_AVAILABLE:
            result = self._analyse_with_networkx(dag_nodes, result)
        else:
            result = self._analyse_with_dfs(dag_nodes, result)

        # Find isolated tasks (no dependencies, no dependents)
        dependents_map: dict[str, list[str]] = {n["task"]: [] for n in dag_nodes}
        for node in dag_nodes:
            for dep in node.get("depends_on", []):
                dependents_map.setdefault(dep, []).append(node["task"])

        result.isolated_tasks = [
            n["task"]
            for n in dag_nodes
            if not n.get("depends_on") and not dependents_map.get(n["task"])
        ]

        if result.isolated_tasks:
            result.warnings.append(
                f"Isolated tasks (no dependencies, no dependents): {result.isolated_tasks}"
            )

        result.is_valid = not result.has_cycles and not result.errors
        return result

    # ── NetworkX Implementation ───────────────────────────────────────────────

    def _analyse_with_networkx(
        self, dag_nodes: list[dict], result: DAGValidationResult
    ) -> DAGValidationResult:
        """Use NetworkX for cycle detection, topological sort, and critical path."""
        G = nx.DiGraph()

        for node in dag_nodes:
            task = node["task"]
            G.add_node(task)
            for dep in node.get("depends_on", []):
                G.add_edge(dep, task)   # dep must complete before task starts

        # Cycle detection
        try:
            cycle = nx.find_cycle(G, orientation="original")
            result.has_cycles = True
            # Extract cycle as a path
            path = [u for u, v, _ in cycle] + [cycle[0][0]]
            result.cycles = [path]
            result.errors.append(
                f"Circular dependency detected: {' -> '.join(path)}"
            )
        except nx.NetworkXNoCycle:
            result.has_cycles = False

        if not result.has_cycles:
            # Topological sort (prerequisites first)
            try:
                result.topological_order = list(nx.topological_sort(G))
            except nx.NetworkXUnfeasible:
                result.errors.append("Topological sort failed — cycle may exist.")

            # Critical path (longest chain)
            try:
                result.critical_path = nx.dag_longest_path(G)
            except Exception as exc:
                logger.debug("Critical path computation failed: %s", exc)
                result.critical_path = []

        return result

    # ── Fallback DFS Implementation (no NetworkX) ────────────────────────────

    def _analyse_with_dfs(
        self, dag_nodes: list[dict], result: DAGValidationResult
    ) -> DAGValidationResult:
        """Pure-Python fallback for cycle detection and topological sort."""
        graph: dict[str, list[str]] = {}
        for node in dag_nodes:
            task = node["task"]
            graph[task] = node.get("depends_on", [])

        # Kahn's algorithm for topological sort
        in_degree: dict[str, int] = {t: 0 for t in graph}
        adjacency: dict[str, list[str]] = {t: [] for t in graph}
        for task, deps in graph.items():
            for dep in deps:
                adjacency.setdefault(dep, []).append(task)
                in_degree[task] = in_degree.get(task, 0) + 1

        queue = [t for t, deg in in_degree.items() if deg == 0]
        topo: list[str] = []

        while queue:
            node = queue.pop(0)
            topo.append(node)
            for neighbour in adjacency.get(node, []):
                in_degree[neighbour] -= 1
                if in_degree[neighbour] == 0:
                    queue.append(neighbour)

        if len(topo) != len(graph):
            # Not all nodes processed → cycle exists
            result.has_cycles = True
            cycle_nodes = [t for t in graph if t not in topo]
            result.cycles = [cycle_nodes]
            result.errors.append(
                f"Circular dependency detected among: {cycle_nodes}"
            )
        else:
            result.topological_order = topo
            # Simple critical path: longest sequence of dependencies
            lengths: dict[str, int] = {}
            predecessors: dict[str, str] = {}
            for task in topo:
                best_len, best_pred = 0, ""
                for dep in graph.get(task, []):
                    dep_len = lengths.get(dep, 0) + 1
                    if dep_len > best_len:
                        best_len, best_pred = dep_len, dep
                lengths[task] = best_len
                predecessors[task] = best_pred

            # Trace back from the node with the maximum length
            if lengths:
                end_node = max(lengths, key=lambda t: lengths[t])
                path = []
                current = end_node
                while current:
                    path.append(current)
                    current = predecessors.get(current, "")
                result.critical_path = list(reversed(path))

        return result

    # ── Convenience Methods ───────────────────────────────────────────────────

    def topological_order(self, dag_nodes: list[dict]) -> list[str]:
        """Return task names in topological order (prerequisites first)."""
        return self.validate(dag_nodes).topological_order

    def detect_cycles(self, dag_nodes: list[dict]) -> list[list[str]]:
        """Return any detected cycles. Empty list means DAG is valid."""
        return self.validate(dag_nodes).cycles

    def critical_path(self, dag_nodes: list[dict]) -> list[str]:
        """Return the critical (longest) path through the DAG — the implementation bottleneck."""
        return self.validate(dag_nodes).critical_path

    def build_servicenow_hierarchy(
        self, dag_nodes: list[dict]
    ) -> list[dict[str, Any]]:
        """
        Convert validated DAG nodes into ServiceNow parent/child task hierarchy.

        Returns a list of dicts with ServiceNow-ready fields:
          {
            "task":       str,       # task description
            "depends_on": list[str], # prerequisite task names
            "snow_state": str,       # "New" for roots, "On Hold" for children
            "snow_parent":str,       # name of parent task (first dependency)
            "snow_blocked_by": list  # all other dependencies
          }
        """
        validation = self.validate(dag_nodes)
        if not validation.is_valid:
            logger.warning("DAG invalid, cannot build ServiceNow hierarchy: %s", validation.errors)
            return []

        result = []
        for node in dag_nodes:
            deps = node.get("depends_on", [])
            snow_state  = "On Hold" if deps else "New"
            snow_parent = deps[0] if deps else ""
            snow_blocked_by = deps[1:] if len(deps) > 1 else []

            result.append({
                "task":             node["task"],
                "depends_on":       deps,
                "snow_state":       snow_state,
                "snow_parent":      snow_parent,
                "snow_blocked_by":  snow_blocked_by,
            })

        return result
