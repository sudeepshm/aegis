"""
knowledge/policy_graph.py
==========================
High-level query layer over the ComplianceOntology.

While ComplianceOntology manages the raw graph, PolicyGraph provides
semantic, business-level queries:

  - find_supersessions()     : "does this circular supersede anything?"
  - find_conflicts()         : "what conflicts with this obligation?"
  - get_compliance_path()    : "what chain of regulations proves compliance?"
  - temporal_snapshot()      : "what was in force on this date?"
  - cross_regulation_check() : "how many regulations cover this topic?"
  - get_enforcement_chain()  : "if A depends on B depends on C, what's the full chain?"

These methods are called by CriticAgent, DeepReasoningAgent, and EvidenceAgent.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Optional

from knowledge.compliance_ontology import (
    ComplianceOntology,
    EdgeType,
    RegEdge,
    RegNode,
)
try:
    from knowledge.rule_compiler import RuleCompiler
    _RC_AVAILABLE = True
except ImportError:
    _RC_AVAILABLE = False

try:
    import yaml as _yaml
    _YAML_AVAILABLE = True
except ImportError:
    _YAML_AVAILABLE = False

_CONTROLS_FILE = Path(__file__).parent / "seeds" / "compensating_controls.yaml"

logger = logging.getLogger(__name__)

# Singleton ontology instance (loaded once at import time)
_GLOBAL_ONTOLOGY: Optional[ComplianceOntology] = None


def get_ontology() -> ComplianceOntology:
    """Return or initialise the singleton ComplianceOntology."""
    global _GLOBAL_ONTOLOGY
    if _GLOBAL_ONTOLOGY is None:
        _GLOBAL_ONTOLOGY = ComplianceOntology()
        logger.info("Singleton ComplianceOntology initialised: %s",
                    _GLOBAL_ONTOLOGY.summary())
    return _GLOBAL_ONTOLOGY


# =============================================================================
# Result Dataclasses
# =============================================================================

@dataclass
class SupersessionResult:
    """Describes a supersession relationship discovered for a regulation."""
    node_id:          str
    reg_id:           str
    title:            str
    superseded_nodes: list[dict[str, Any]] = field(default_factory=list)
    superseded_by:    list[dict[str, Any]] = field(default_factory=list)
    is_current:       bool = True
    notes:            list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id":          self.node_id,
            "reg_id":           self.reg_id,
            "title":            self.title,
            "superseded_nodes": self.superseded_nodes,
            "superseded_by":    self.superseded_by,
            "is_current":       self.is_current,
            "notes":            self.notes,
        }


@dataclass
class ConflictReport:
    """Describes a regulatory conflict discovered for an obligation."""
    obligation_text:  str
    domain:           str
    conflicts:        list[dict[str, Any]] = field(default_factory=list)
    conflict_count:   int = 0
    highest_severity: str = "LOW"
    resolution_notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "obligation_text":  self.obligation_text,
            "domain":           self.domain,
            "conflicts":        self.conflicts,
            "conflict_count":   self.conflict_count,
            "highest_severity": self.highest_severity,
            "resolution_notes": self.resolution_notes,
        }


@dataclass
class CompliancePath:
    """Chain of regulatory dependencies that must all be satisfied."""
    start_node:     str
    end_node:       str
    path_nodes:     list[dict[str, Any]] = field(default_factory=list)
    path_length:    int = 0
    all_mandatory:  bool = True
    blocking_nodes: list[str] = field(default_factory=list)  # non-mandatory gaps

    def to_dict(self) -> dict[str, Any]:
        return {
            "start_node":    self.start_node,
            "end_node":      self.end_node,
            "path_nodes":    self.path_nodes,
            "path_length":   self.path_length,
            "all_mandatory": self.all_mandatory,
            "blocking_nodes": self.blocking_nodes,
        }


@dataclass
class RegulatorySnapshot:
    """Active regulations on a specific date."""
    as_of_date: str
    active_nodes: list[dict[str, Any]] = field(default_factory=list)
    total_count:  int = 0
    by_source:    dict[str, int] = field(default_factory=dict)
    by_domain:    dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "as_of_date":   self.as_of_date,
            "total_count":  self.total_count,
            "by_source":    self.by_source,
            "by_domain":    self.by_domain,
            "active_nodes": self.active_nodes,
        }


@dataclass
class CrossRegulationReport:
    """How many regulations cover a given topic/domain."""
    topic:            str
    domain:           str
    regulations:      list[dict[str, Any]] = field(default_factory=list)
    count:            int = 0
    risk_amplifier:   float = 1.0  # multiplier for risk scoring when multiple regs cover same topic
    unique_sources:   list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "topic":          self.topic,
            "domain":         self.domain,
            "count":          self.count,
            "risk_amplifier": round(self.risk_amplifier, 3),
            "unique_sources": self.unique_sources,
            "regulations":    self.regulations,
        }


# =============================================================================
# Phase 9 — Feature 2: Policy Tension Detection Data Structures
# =============================================================================

@dataclass
class TensionPair:
    """A detected semantic tension between two regulatory obligations."""
    obligation_a_text: str     # incoming obligation
    obligation_b_text: str     # existing ontology node summary
    regulation_a:      str     # reg_id of incoming obligation's regulation
    regulation_b:      str     # reg_id of the conflicting ontology node
    verb_a:            str     # action verb from obligation A
    verb_b:            str     # action verb from obligation B
    shared_object:     str     # the object both verbs act upon
    domain:            str
    tension_id:        str = ""  # matches a key in compensating_controls.yaml

    def to_dict(self) -> dict[str, Any]:
        return {
            "obligation_a_text": self.obligation_a_text[:150],
            "obligation_b_text": self.obligation_b_text[:150],
            "regulation_a":      self.regulation_a,
            "regulation_b":      self.regulation_b,
            "verb_a":            self.verb_a,
            "verb_b":            self.verb_b,
            "shared_object":     self.shared_object,
            "domain":            self.domain,
            "tension_id":        self.tension_id,
        }


@dataclass
class PolicyTensionReport:
    """Full tension detection result for an obligation."""
    obligation_text:       str
    has_tension:           bool = False
    tension_pairs:         list[TensionPair] = field(default_factory=list)
    compensating_controls: list[str] = field(default_factory=list)
    affected_regulations:  list[str] = field(default_factory=list)
    summary:               str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "obligation_text":       self.obligation_text[:200],
            "has_tension":           self.has_tension,
            "tension_pairs":         [t.to_dict() for t in self.tension_pairs],
            "compensating_controls": self.compensating_controls,
            "affected_regulations":  self.affected_regulations,
            "summary":               self.summary,
        }


# =============================================================================
# Policy Graph
# =============================================================================

class PolicyGraph:
    """
    Semantic query layer over ComplianceOntology.

    Usage::

        pg = PolicyGraph()
        # or: pg = PolicyGraph(ontology=my_custom_ontology)

        supersession = pg.find_supersessions("rbi_it_framework_2023")
        conflicts    = pg.find_conflicts("encryption", "Banks shall use AES-256")
        path         = pg.get_compliance_path("rbi_kyc_master_2016", "rbi_aml_cft_2023")
    """

    def __init__(self, ontology: Optional[ComplianceOntology] = None):
        self._onto = ontology or get_ontology()

    # ── Supersession Queries ──────────────────────────────────────────────────

    def find_supersessions(self, node_id_or_reg_id: str) -> SupersessionResult:
        """
        Determine if a regulation supersedes older ones or has been superseded.

        Returns a SupersessionResult describing:
          - Which older regulations this node replaced
          - Whether this node has itself been superseded (by newer regs)
          - Whether the node is currently active
        """
        # Resolve node
        node = (
            self._onto.get_node(node_id_or_reg_id)
            or self._onto.get_node_by_reg_id(node_id_or_reg_id)
        )
        if not node:
            logger.warning("find_supersessions: node '%s' not found", node_id_or_reg_id)
            return SupersessionResult(
                node_id=node_id_or_reg_id,
                reg_id=node_id_or_reg_id,
                title="Unknown",
                is_current=False,
                notes=[f"Node '{node_id_or_reg_id}' not found in ontology"],
            )

        # What this node supersedes (older)
        superseded = self._onto.get_supersedes(node.id)
        superseded_dicts = [
            {
                "id":             n.id,
                "reg_id":         n.reg_id,
                "title":          n.title,
                "expiry_date":    n.expiry_date.isoformat() if n.expiry_date else None,
                "penalty_severity": n.penalty_severity,
            }
            for n in superseded
        ]

        # What supersedes this node (newer)
        superseded_by = self._onto.get_superseded_by(node.id)
        superseded_by_dicts = [
            {
                "id":             n.id,
                "reg_id":         n.reg_id,
                "title":          n.title,
                "effective_date": n.effective_date.isoformat(),
            }
            for n in superseded_by
        ]

        is_current = node.is_active() and len(superseded_by) == 0
        notes: list[str] = []
        if superseded:
            notes.append(
                f"This regulation supersedes {len(superseded)} older obligation(s): "
                + ", ".join(n.reg_id for n in superseded)
            )
        if superseded_by:
            notes.append(
                f"This regulation has been superseded by: "
                + ", ".join(n.reg_id for n in superseded_by)
            )
        if not node.is_active():
            notes.append(f"This regulation expired on {node.expiry_date}")

        return SupersessionResult(
            node_id          = node.id,
            reg_id           = node.reg_id,
            title            = node.title,
            superseded_nodes = superseded_dicts,
            superseded_by    = superseded_by_dicts,
            is_current       = is_current,
            notes            = notes,
        )

    # ── Conflict Queries ──────────────────────────────────────────────────────

    def find_conflicts(self, domain: str, obligation_text: str) -> ConflictReport:
        """
        Find regulatory conflicts relevant to an obligation within a domain.

        Used by CriticAgent to flag contradictory requirements.

        Example:
            find_conflicts("data_protection", "retain customer data for 5 years")
            → returns conflict between DPDP erasure (Section 8(7)) and RBI AML 5-year retention
        """
        raw_conflicts = self._onto.find_conflicting_standards(domain, obligation_text)

        if not raw_conflicts:
            return ConflictReport(
                obligation_text=obligation_text,
                domain=domain,
                conflicts=[],
                conflict_count=0,
            )

        # Determine highest severity across all conflicting nodes
        severity_order = {"HIGH": 3, "MEDIUM": 2, "LOW": 1}
        max_severity = "LOW"
        for conflict in raw_conflicts:
            for side in ["node_a", "node_b"]:
                sev = conflict.get(side, {}).get("penalty_severity", "LOW")
                if severity_order.get(sev, 0) > severity_order.get(max_severity, 0):
                    max_severity = sev

        # Build resolution notes
        resolution_parts = []
        for conflict in raw_conflicts[:3]:  # Top 3 most relevant
            notes = conflict.get("notes", "")
            if notes:
                resolution_parts.append(f"• {notes}")

        return ConflictReport(
            obligation_text  = obligation_text,
            domain           = domain,
            conflicts        = raw_conflicts,
            conflict_count   = len(raw_conflicts),
            highest_severity = max_severity,
            resolution_notes = "\n".join(resolution_parts),
        )

    def find_conflicts_for_node(self, node_id: str) -> ConflictReport:
        """Find conflicts for a specific known regulation node."""
        node = self._onto.get_node(node_id)
        if not node:
            return ConflictReport(
                obligation_text=node_id,
                domain="unknown",
                notes=[f"Node '{node_id}' not found"],
            )

        conflict_pairs = self._onto.get_conflicts(node_id)
        conflicts = [
            {
                "node_a":    node.to_dict(),
                "node_b":    conflicting.to_dict(),
                "notes":     notes,
                "relevance": 1.0,
            }
            for conflicting, notes in conflict_pairs
        ]

        severity_order = {"HIGH": 3, "MEDIUM": 2, "LOW": 1}
        max_severity = max(
            (c["node_b"].get("penalty_severity", "LOW") for c in conflicts),
            key=lambda s: severity_order.get(s, 0),
            default="LOW",
        )

        return ConflictReport(
            obligation_text  = f"{node.reg_id}: {node.summary}",
            domain           = node.domain,
            conflicts        = conflicts,
            conflict_count   = len(conflicts),
            highest_severity = max_severity,
            resolution_notes = "\n".join(
                f"• {c['notes']}" for c in conflicts if c.get("notes")
            ),
        )

    # ── Compliance Path Queries ───────────────────────────────────────────────

    def get_compliance_path(self, start_id: str, end_id: str) -> CompliancePath:
        """
        Find the shortest relationship path between two regulations.
        Answers: "How does obligation A relate to obligation B through the graph?"
        """
        path_ids = self._onto.get_shortest_compliance_path(start_id, end_id)

        if not path_ids:
            return CompliancePath(
                start_node=start_id,
                end_node=end_id,
                path_nodes=[],
                path_length=0,
            )

        path_nodes = []
        all_mandatory = True
        blocking: list[str] = []
        for nid in path_ids:
            n = self._onto.get_node(nid)
            if n:
                path_nodes.append(n.to_dict())
                if not n.mandatory:
                    all_mandatory = False
                    blocking.append(nid)
            else:
                path_nodes.append({"id": nid, "title": "Unknown"})

        return CompliancePath(
            start_node     = start_id,
            end_node       = end_id,
            path_nodes     = path_nodes,
            path_length    = len(path_ids) - 1,
            all_mandatory  = all_mandatory,
            blocking_nodes = blocking,
        )

    def get_full_dependency_chain(self, node_id: str) -> list[dict[str, Any]]:
        """
        Get all DEPENDS_ON dependencies recursively (the full prerequisite chain).
        Returns nodes in order: most fundamental first.
        """
        visited: set[str] = set()
        result: list[RegNode] = []

        def _dfs(nid: str) -> None:
            if nid in visited:
                return
            visited.add(nid)
            deps = self._onto.get_dependencies(nid)
            for dep in deps:
                _dfs(dep.id)
            node = self._onto.get_node(nid)
            if node and nid != node_id:
                result.append(node)

        _dfs(node_id)
        return [n.to_dict() for n in result]

    # ── Temporal Queries ──────────────────────────────────────────────────────

    def temporal_snapshot(self, as_of: Optional[date] = None) -> RegulatorySnapshot:
        """
        Return all regulations that were in force on `as_of` date.
        Used by TemporalReasoner for historical compliance verification.
        """
        check_date = as_of or date.today()
        active = self._onto.get_all_nodes(active_only=True, as_of=check_date)

        by_source: dict[str, int] = {}
        by_domain: dict[str, int] = {}
        for n in active:
            by_source[n.source] = by_source.get(n.source, 0) + 1
            by_domain[n.domain] = by_domain.get(n.domain, 0) + 1

        return RegulatorySnapshot(
            as_of_date   = check_date.isoformat(),
            active_nodes = [n.to_dict() for n in active],
            total_count  = len(active),
            by_source    = by_source,
            by_domain    = by_domain,
        )

    def was_active_on(self, node_id: str, check_date: date) -> bool:
        """Return True if the regulation was in force on the given date."""
        node = self._onto.get_node(node_id)
        if not node:
            return False
        return node.is_active(check_date)

    # ── Cross-Regulation Coverage ─────────────────────────────────────────────

    def cross_regulation_coverage(self, topic: str, domain: str) -> CrossRegulationReport:
        """
        Determine how many different regulations cover a specific topic.
        High coverage → higher risk amplifier (multiple regulators watching).

        Used by RiskScoringEngine.
        """
        keyword_nodes = self._onto.find_nodes_by_keyword(topic)
        domain_nodes  = self._onto.get_nodes_by_domain(domain)

        # Union: nodes matching keyword OR in the domain
        all_ids = set(n.id for n in keyword_nodes) | set(n.id for n in domain_nodes)
        relevant_nodes = [
            self._onto.get_node(nid) for nid in all_ids
            if self._onto.get_node(nid) and self._onto.get_node(nid).is_active()
        ]
        relevant_nodes = [n for n in relevant_nodes if n]  # type: ignore

        unique_sources = list(set(n.source for n in relevant_nodes))
        count = len(relevant_nodes)

        # Risk amplifier: more sources = higher regulatory scrutiny
        # 1 source → 1.0x, 2 sources → 1.3x, 3+ sources → 1.6x
        if len(unique_sources) >= 3:
            risk_amplifier = 1.6
        elif len(unique_sources) == 2:
            risk_amplifier = 1.3
        else:
            risk_amplifier = 1.0

        return CrossRegulationReport(
            topic          = topic,
            domain         = domain,
            regulations    = [n.to_dict() for n in relevant_nodes],
            count          = count,
            risk_amplifier = risk_amplifier,
            unique_sources = unique_sources,
        )

    # ── Related Obligations ───────────────────────────────────────────────────

    def get_related_obligations(
        self, node_id: str, max_hops: int = 2
    ) -> list[dict[str, Any]]:
        """
        Return all regulations related within `max_hops` graph hops.
        Used by CriticAgent to build the context window before critique.
        """
        related = self._onto.get_all_related(node_id, max_hops=max_hops)
        return [n.to_dict() for n in related]

    # ── Regulatory Clusters ───────────────────────────────────────────────────

    def get_domain_cluster(self, domain: str) -> list[dict[str, Any]]:
        """Return all regulations in a domain cluster with their relationships."""
        nodes = self._onto.get_nodes_by_domain(domain)
        result = []
        for node in nodes:
            conflicts = self._onto.get_conflicts(node.id)
            deps = self._onto.get_dependencies(node.id)
            result.append({
                **node.to_dict(),
                "conflicts_with": [c.id for c, _ in conflicts],
                "depends_on":     [d.id for d in deps],
            })
        return result

    def search_by_keyword(self, keyword: str) -> list[dict[str, Any]]:
        """Full-text search across all regulatory nodes."""
        return [n.to_dict() for n in self._onto.find_nodes_by_keyword(keyword)]

    # ==========================================================================
    # Phase 9 — Feature 2: Semantic Policy Tension Detection
    # ==========================================================================

    # Semantic opposition map: verb A vs list of opposing verbs
    _SEMANTIC_OPPOSITES: dict[str, list[str]] = {
        "retain":   ["delete", "erase", "purge", "minimize", "destroy", "remove"],
        "maximize": ["minimize", "reduce", "limit", "restrict", "cap"],
        "encrypt":  ["share", "expose", "transmit", "broadcast", "publish"],
        "restrict": ["allow", "permit", "open", "enable", "grant", "authorise"],
        "log":      ["suppress", "disable", "block", "prevent"],
        "share":    ["encrypt", "restrict", "limit", "withhold"],
        "delete":   ["retain", "store", "archive", "preserve"],
        "minimize": ["maximize", "retain", "store", "expand", "increase"],
    }

    def detect_policy_tension(
        self,
        obligation_text: str,
        regulation_ref: str = "",
        domain: str = "",
    ) -> PolicyTensionReport:
        """
        Phase 9 — Feature 2: Detect semantic tensions between an incoming
        obligation and existing regulatory obligations in the ontology.

        Algorithm:
          1. Extract (verb, object) from the obligation using RuleCompiler
          2. Query all active ontology nodes in the same domain
          3. For each existing node, extract its verb from the summary
          4. If verb_a and verb_b are semantic opposites AND they act on the
             same object → emit TensionPair
          5. Load compensating controls from the YAML seed

        Returns a PolicyTensionReport.
        """
        report = PolicyTensionReport(obligation_text=obligation_text)

        # Step 1: Extract action verb and object from the incoming obligation
        incoming_verb, incoming_object = self._extract_verb_object(obligation_text)
        if not incoming_verb:
            report.summary = "Could not extract action verb from obligation text."
            return report

        # Step 2: Find all active domain nodes
        domain_nodes = self._onto.get_all_nodes(active_only=True)

        # Step 3: Check each node for semantic tension
        tension_templates = self._load_compensating_controls()

        for node in domain_nodes:
            existing_verb, existing_object = self._extract_verb_object(
                f"{node.summary} {' '.join(node.clauses[:2])}"
            )
            if not existing_verb:
                continue

            # Step 4: Check semantic opposition
            opposites_of_incoming = self._SEMANTIC_OPPOSITES.get(incoming_verb, [])
            if existing_verb not in opposites_of_incoming:
                # Also check reverse direction
                opposites_of_existing = self._SEMANTIC_OPPOSITES.get(existing_verb, [])
                if incoming_verb not in opposites_of_existing:
                    continue

            # Check shared object (at least one keyword must overlap)
            shared_obj = self._find_shared_object(
                incoming_object, existing_object, obligation_text, node.summary
            )
            if not shared_obj:
                continue

            # Find matching tension template
            tension_id = self._find_tension_template_id(
                incoming_verb, existing_verb, shared_obj, tension_templates
            )

            pair = TensionPair(
                obligation_a_text = obligation_text,
                obligation_b_text = node.summary,
                regulation_a      = regulation_ref or "Incoming Obligation",
                regulation_b      = node.reg_id,
                verb_a            = incoming_verb,
                verb_b            = existing_verb,
                shared_object     = shared_obj,
                domain            = node.domain,
                tension_id        = tension_id,
            )
            report.tension_pairs.append(pair)

            if node.reg_id not in report.affected_regulations:
                report.affected_regulations.append(node.reg_id)

            # Attach compensating controls
            controls = self._get_controls_for_tension(tension_id, tension_templates)
            for ctrl in controls:
                if ctrl not in report.compensating_controls:
                    report.compensating_controls.append(ctrl)

        report.has_tension = len(report.tension_pairs) > 0

        if report.has_tension:
            reg_list = ", ".join(report.affected_regulations[:3])
            report.summary = (
                f"POLICY TENSION detected: '{obligation_text[:80]}' creates semantic "
                f"conflict with {len(report.tension_pairs)} existing obligation(s) "
                f"({reg_list}). {len(report.compensating_controls)} compensating "
                f"control(s) identified."
            )
        else:
            report.summary = "No policy tensions detected."

        return report

    # ── Tension Detection Helpers ───────────────────────────────────────────

    @staticmethod
    def _extract_verb_object(text: str) -> tuple[str, str]:
        """
        Extract the primary action verb and its object from obligation text.

        Uses RuleCompiler if available, otherwise simple keyword scan.
        Returns (verb, object) or ("", "") if extraction fails.
        """
        if _RC_AVAILABLE:
            try:
                rc = RuleCompiler()
                rule = rc.compile(text)
                return rule.verb.lower(), rule.object.lower()
            except Exception:
                pass

        # Fallback: keyword scan against known verbs
        text_lower = text.lower()
        all_verbs = set(PolicyGraph._SEMANTIC_OPPOSITES.keys())
        for opp_list in PolicyGraph._SEMANTIC_OPPOSITES.values():
            all_verbs.update(opp_list)

        found_verb = ""
        for verb in sorted(all_verbs, key=len, reverse=True):  # longer verbs first
            if verb in text_lower:
                found_verb = verb
                break

        # Extract a simple object: words following the verb
        if found_verb:
            idx = text_lower.find(found_verb)
            rest = text_lower[idx + len(found_verb):].strip().split()
            obj = " ".join(rest[:3]) if rest else "data"
            return found_verb, obj

        return "", ""

    @staticmethod
    def _find_shared_object(
        obj_a: str, obj_b: str, text_a: str, text_b: str
    ) -> str:
        """
        Return a shared object keyword if both obligations act on the same target.
        Checks explicit objects first, then falls back to keyword overlap.
        """
        # Common data object keywords
        data_keywords = [
            "customer", "data", "log", "record", "information", "personal",
            "transaction", "report", "document", "audit", "access",
            "capital", "reserve", "buffer", "encryption", "key",
        ]
        text_a_l = (obj_a + " " + text_a).lower()
        text_b_l = (obj_b + " " + text_b).lower()

        for kw in data_keywords:
            if kw in text_a_l and kw in text_b_l:
                return kw
        return ""

    @staticmethod
    def _load_compensating_controls() -> list[dict]:
        """Load compensating control templates from the YAML seed file."""
        if not _YAML_AVAILABLE or not _CONTROLS_FILE.exists():
            return []
        try:
            with _CONTROLS_FILE.open("r", encoding="utf-8") as fh:
                data = _yaml.safe_load(fh)
            return data.get("tensions", [])
        except Exception as exc:
            logger.warning("Failed to load compensating_controls.yaml: %s", exc)
            return []

    @staticmethod
    def _find_tension_template_id(
        verb_a: str, verb_b: str, shared_obj: str, templates: list[dict]
    ) -> str:
        """Match the detected tension to a template ID."""
        for template in templates:
            tv_a = template.get("verb_a", "")
            tv_b = template.get("verb_b", "")
            obj_kws = template.get("object_keywords", [])

            verb_match = (
                (tv_a == verb_a and tv_b == verb_b) or
                (tv_a == verb_b and tv_b == verb_a)
            )
            obj_match = any(kw in shared_obj for kw in obj_kws)

            if verb_match and obj_match:
                return template.get("id", "")
        return ""

    @staticmethod
    def _get_controls_for_tension(
        tension_id: str, templates: list[dict]
    ) -> list[str]:
        """Return compensating controls for a given tension ID."""
        if not tension_id:
            return []
        for template in templates:
            if template.get("id") == tension_id:
                return template.get("compensating_controls", [])
        return []
