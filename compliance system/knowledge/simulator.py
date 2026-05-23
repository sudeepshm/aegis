"""
knowledge/simulator.py
=======================
Phase 9 — Feature 3: Counterfactual "What-If" Blast Radius Simulator.

Exposed via the /api/v1/simulate_delay endpoint (on-demand only — approved Option B).

Given a validated DAG and a hypothetical task delay, computes:
  - Which downstream tasks are affected (cascade)
  - Which regulatory deadlines are breached
  - The resulting risk score delta
  - A human-readable narrative for the Chief Compliance Officer

Design
------
  BlastRadiusSimulator.simulate_delay(dag, delayed_task, delay_days, deadlines)
    → BlastRadiusReport

Uses NetworkX for DAG traversal (descendants) and critical path analysis.
Falls back to pure-Python BFS if NetworkX is unavailable.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Optional

logger = logging.getLogger(__name__)

try:
    import networkx as nx
    _NX_AVAILABLE = True
except ImportError:
    _NX_AVAILABLE = False
    logger.warning("networkx not available — BlastRadiusSimulator will use BFS fallback")


# =============================================================================
# Data Structures
# =============================================================================

@dataclass
class CascadeImpact:
    """A single downstream task impacted by the delay."""
    task_name:           str
    original_start_day:  int     # days from project start
    delayed_start_day:   int     # days from project start after delay
    days_delayed:        int
    is_on_critical_path: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_name":           self.task_name,
            "original_start_day":  self.original_start_day,
            "delayed_start_day":   self.delayed_start_day,
            "days_delayed":        self.days_delayed,
            "is_on_critical_path": self.is_on_critical_path,
        }


@dataclass
class DeadlineBreach:
    """A regulatory deadline that will be missed due to the cascade delay."""
    task_name:        str
    regulatory_ref:   str
    original_deadline: date
    projected_completion: date
    breach_days:      int     # how many days past the deadline
    penalty_tier:     str     # "HIGH" | "MEDIUM" | "LOW"
    penalty_narrative: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_name":            self.task_name,
            "regulatory_ref":       self.regulatory_ref,
            "original_deadline":    self.original_deadline.isoformat(),
            "projected_completion": self.projected_completion.isoformat(),
            "breach_days":          self.breach_days,
            "penalty_tier":         self.penalty_tier,
            "penalty_narrative":    self.penalty_narrative,
        }


@dataclass
class BlastRadiusReport:
    """Full counterfactual impact report for a hypothetical task delay."""
    delayed_task:       str
    delay_days:         int
    cascade_tasks:      list[CascadeImpact] = field(default_factory=list)
    breached_deadlines: list[DeadlineBreach] = field(default_factory=list)
    risk_delta_pct:     float = 0.0     # % increase in overall risk score
    total_tasks_impacted: int = 0
    critical_path_broken: bool = False
    penalty_estimate:   str = ""
    narrative:          str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "delayed_task":         self.delayed_task,
            "delay_days":           self.delay_days,
            "total_tasks_impacted": self.total_tasks_impacted,
            "critical_path_broken": self.critical_path_broken,
            "risk_delta_pct":       round(self.risk_delta_pct, 2),
            "penalty_estimate":     self.penalty_estimate,
            "narrative":            self.narrative,
            "cascade_tasks":        [c.to_dict() for c in self.cascade_tasks],
            "breached_deadlines":   [b.to_dict() for b in self.breached_deadlines],
            # Compatibility/Feature mappings for moderate tier M-4:
            "affected_tasks":       [c.task_name for c in self.cascade_tasks],
            "deadline_breaches":    [b.to_dict() for b in self.breached_deadlines],
            "risk_delta":           round(self.risk_delta_pct, 2),
        }


# =============================================================================
# Blast Radius Simulator
# =============================================================================

class BlastRadiusSimulator:
    """
    Counterfactual "What-If" blast radius simulator for compliance task DAGs.

    Given a compliance task DAG (from DeepReasoningAgent.dag_nodes) and a
    hypothetical delay scenario, computes the full downstream impact.

    This is an ANALYTICAL TOOL exposed only as an on-demand API endpoint
    (/api/v1/simulate_delay) — it does NOT run inside the main ingestion pipeline
    to avoid latency bottlenecks (Approved: Option B).

    Usage::
        sim = BlastRadiusSimulator()
        report = sim.simulate_delay(
            dag           = dag_nodes,          # from ReasoningResult.dag_nodes
            delayed_task  = "Upgrade IAM to v2",
            delay_days    = 30,
            deadlines     = {"Enable MFA": date(2024, 6, 1)},
        )
        print(report.narrative)
    """

    # Penalty narrative templates by tier
    _PENALTY_TEMPLATES = {
        "HIGH": (
            "Regulatory breach of {days} day(s) on '{task}' is likely to attract "
            "direct regulatory action under {ref}. Estimated penalties: monetary fine "
            "(up to ₹5 Cr per instance), mandatory reporting to RBI/SEBI Board, "
            "and potential suspension of affected business lines."
        ),
        "MEDIUM": (
            "Breach of {days} day(s) on '{task}' under {ref} may trigger a regulatory "
            "show-cause notice. Recommended action: proactive disclosure to regulator "
            "with a remediation timeline to mitigate penalty risk."
        ),
        "LOW": (
            "Minor delay of {days} day(s) on '{task}' under {ref}. "
            "Document in the compliance register with a revised target date. "
            "Consider filing a self-declaration if required by the regulation."
        ),
    }

    def simulate_delay(
        self,
        dag: list[dict],
        delayed_task: str,
        delay_days: int,
        regulation_deadlines: Optional[dict[str, date]] = None,
        task_duration_days: int = 5,    # assumed duration per task
        base_risk_score: float = 5.0,   # baseline 0-10 risk score
    ) -> BlastRadiusReport:
        """
        Simulate the blast radius of delaying `delayed_task` by `delay_days`.

        Algorithm:
          1. Build NetworkX DiGraph from dag nodes
          2. Find all downstream tasks (descendants of delayed_task)
          3. Compute new start dates for each downstream task
          4. Compare projected completion vs regulation_deadlines
          5. Score risk delta based on number of breached deadlines
          6. Generate narrative

        Args:
            dag:                  Validated DAG from DeepReasoningAgent.dag_nodes
            delayed_task:         Name of the task being hypothetically delayed
            delay_days:           Number of days the task is delayed
            regulation_deadlines: {task_name → deadline_date} from TemporalReasoner
            task_duration_days:   Assumed duration per task (default 5 days)
            base_risk_score:      Current risk score (0-10) to compute delta from

        Returns a BlastRadiusReport.
        """
        report = BlastRadiusReport(
            delayed_task = delayed_task,
            delay_days   = delay_days,
        )

        deadlines = regulation_deadlines or {}

        # Validate delayed_task is in the DAG
        task_names = {n.get("task", "") for n in dag}
        if delayed_task not in task_names:
            report.narrative = (
                f"Task '{delayed_task}' not found in the DAG. "
                f"Available tasks: {sorted(task_names)}"
            )
            return report

        if delay_days <= 0:
            report.narrative = "delay_days must be > 0."
            return report

        # Build execution schedule (topological order with start days)
        schedule = self._build_schedule(dag, task_duration_days)

        # Find downstream tasks affected by the delay
        downstream = self._find_downstream(dag, delayed_task)

        # Build critical path for context
        critical_path = self._find_critical_path(dag)

        # Compute cascade impacts
        for task_name in downstream:
            orig_start = schedule.get(task_name, 0)
            delayed_start = orig_start + delay_days

            cascade = CascadeImpact(
                task_name           = task_name,
                original_start_day  = orig_start,
                delayed_start_day   = delayed_start,
                days_delayed        = delay_days,
                is_on_critical_path = task_name in critical_path,
            )
            report.cascade_tasks.append(cascade)

            if cascade.is_on_critical_path:
                report.critical_path_broken = True

            # Check for deadline breaches
            if task_name in deadlines:
                projected_completion = (
                    date.today()
                    + timedelta(days=delayed_start + task_duration_days)
                )
                deadline = deadlines[task_name]
                if projected_completion > deadline:
                    breach_days = (projected_completion - deadline).days
                    penalty_tier = (
                        "HIGH" if breach_days > 30 else
                        "MEDIUM" if breach_days > 7 else "LOW"
                    )
                    template = self._PENALTY_TEMPLATES.get(penalty_tier, "")
                    breach = DeadlineBreach(
                        task_name         = task_name,
                        regulatory_ref    = f"Regulation covering '{task_name}'",
                        original_deadline = deadline,
                        projected_completion = projected_completion,
                        breach_days       = breach_days,
                        penalty_tier      = penalty_tier,
                        penalty_narrative = template.format(
                            days=breach_days,
                            task=task_name,
                            ref=f"Regulation covering '{task_name}'",
                        ),
                    )
                    report.breached_deadlines.append(breach)

        report.total_tasks_impacted = len(report.cascade_tasks)

        # Risk delta: each HIGH breach = +20%, MEDIUM = +10%, LOW = +5%
        delta = 0.0
        for breach in report.breached_deadlines:
            delta += {"HIGH": 20.0, "MEDIUM": 10.0, "LOW": 5.0}.get(breach.penalty_tier, 5.0)
        if report.critical_path_broken:
            delta += 15.0  # critical path penalty
        report.risk_delta_pct = min(delta, 100.0)

        # Penalty estimate
        if report.breached_deadlines:
            high_breaches = [b for b in report.breached_deadlines if b.penalty_tier == "HIGH"]
            report.penalty_estimate = (
                f"{len(report.breached_deadlines)} regulatory deadline(s) breached. "
                + (
                    f"{len(high_breaches)} HIGH-severity breach(es) may attract monetary fines. "
                    if high_breaches else ""
                )
                + f"Overall risk score increase: +{report.risk_delta_pct:.1f}%."
            )
        else:
            report.penalty_estimate = (
                f"No regulatory deadlines breached. "
                f"Risk score increase from cascade delays: +{report.risk_delta_pct:.1f}%."
            )

        # Narrative
        cascade_names = [c.task_name for c in report.cascade_tasks[:5]]
        report.narrative = (
            f"Delaying '{delayed_task}' by {delay_days} day(s) cascades to "
            f"{report.total_tasks_impacted} downstream task(s): "
            f"{', '.join(cascade_names)}"
            + (f"..." if len(report.cascade_tasks) > 5 else ".")
            + (f" Critical path BROKEN — overall project completion delayed by {delay_days}+ days."
               if report.critical_path_broken else "")
            + (f" {len(report.breached_deadlines)} regulatory deadline(s) will be missed."
               if report.breached_deadlines else " No regulatory deadlines breached.")
            + f" Risk score delta: +{report.risk_delta_pct:.1f}%."
        )

        return report

    # ── Internal Helpers ──────────────────────────────────────────────────────

    def _build_schedule(
        self, dag: list[dict], task_duration: int
    ) -> dict[str, int]:
        """
        Compute earliest start day for each task via topological traversal.
        Returns {task_name: start_day}.
        """
        node_map = {n["task"]: n for n in dag}
        schedule: dict[str, int] = {}

        def _start_day(task: str, visited: set) -> int:
            if task in schedule:
                return schedule[task]
            if task in visited:
                return 0  # cycle guard
            visited.add(task)
            deps = node_map.get(task, {}).get("depends_on", [])
            if not deps:
                schedule[task] = 0
            else:
                schedule[task] = max(
                    _start_day(dep, visited) + task_duration for dep in deps
                )
            return schedule[task]

        for node in dag:
            _start_day(node["task"], set())

        return schedule

    def _find_downstream(self, dag: list[dict], task_name: str) -> list[str]:
        """Find all tasks downstream of task_name (i.e., blocked by it)."""
        if _NX_AVAILABLE:
            G = nx.DiGraph()
            for node in dag:
                for dep in node.get("depends_on", []):
                    G.add_edge(dep, node["task"])
            if task_name not in G:
                return []
            try:
                return list(nx.descendants(G, task_name))
            except Exception:
                pass

        # BFS fallback
        adjacency: dict[str, list[str]] = {n["task"]: [] for n in dag}
        for node in dag:
            for dep in node.get("depends_on", []):
                adjacency.setdefault(dep, []).append(node["task"])

        visited, queue = set(), [task_name]
        while queue:
            current = queue.pop(0)
            for child in adjacency.get(current, []):
                if child not in visited:
                    visited.add(child)
                    queue.append(child)
        visited.discard(task_name)
        return list(visited)

    def _find_critical_path(self, dag: list[dict]) -> list[str]:
        """Return the critical (longest) path through the DAG."""
        if _NX_AVAILABLE:
            G = nx.DiGraph()
            for node in dag:
                for dep in node.get("depends_on", []):
                    G.add_edge(dep, node["task"])
            try:
                return nx.dag_longest_path(G)
            except Exception:
                pass
        return []
