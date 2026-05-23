"""
knowledge/temporal_reasoner.py
================================
Date-aware obligation validity and supersession timeline reasoning.

Answers questions like:
  - "Was this regulation in force on 2023-06-01?"
  - "What obligations applied to banks in the AML domain as of 2022?"
  - "When was the incident reporting window reduced from 72h to 6h?"
  - "Which regulations have changed in the last 90 days?"

Used by:
  - CriticAgent: to detect if an obligation cites a superseded regulation
  - DeepReasoningAgent: to flag temporal compliance risks
  - ValidationLayer: to verify evidence timestamps match applicable regulation period
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Optional

from knowledge.compliance_ontology import ComplianceOntology, EdgeType, RegNode
from knowledge.policy_graph import get_ontology

logger = logging.getLogger(__name__)


# =============================================================================
# Data Structures
# =============================================================================

@dataclass
class ObligationValidity:
    """Validity assessment for a specific regulation at a specific date."""
    node_id:         str
    reg_id:          str
    title:           str
    check_date:      str
    is_valid:        bool
    effective_from:  str
    effective_until: Optional[str]
    superseded_by:   list[dict[str, Any]] = field(default_factory=list)
    supersedes:      list[dict[str, Any]] = field(default_factory=list)
    notes:           list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id":         self.node_id,
            "reg_id":          self.reg_id,
            "title":           self.title,
            "check_date":      self.check_date,
            "is_valid":        self.is_valid,
            "effective_from":  self.effective_from,
            "effective_until": self.effective_until,
            "superseded_by":   self.superseded_by,
            "supersedes":      self.supersedes,
            "notes":           self.notes,
        }


@dataclass
class RegulatoryTimeline:
    """Timeline of regulatory changes in a domain."""
    domain:        str
    source:        Optional[str]
    start_date:    str
    end_date:      str
    events:        list[dict[str, Any]] = field(default_factory=list)
    change_count:  int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "domain":       self.domain,
            "source":       self.source,
            "start_date":   self.start_date,
            "end_date":     self.end_date,
            "event_count":  self.change_count,
            "events":       self.events,
        }


@dataclass
class SupersessionEvent:
    """A single supersession event in the regulatory timeline."""
    event_date:   str
    event_type:   str      # "NEW_REGULATION" | "SUPERSESSION" | "EXPIRY"
    old_reg_id:   Optional[str]
    new_reg_id:   Optional[str]
    old_title:    Optional[str]
    new_title:    Optional[str]
    impact:       str      # key change summary
    domain:       str
    source:       str

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_date": self.event_date,
            "event_type": self.event_type,
            "old_reg_id": self.old_reg_id,
            "new_reg_id": self.new_reg_id,
            "old_title":  self.old_title,
            "new_title":  self.new_title,
            "impact":     self.impact,
            "domain":     self.domain,
            "source":     self.source,
        }


@dataclass
class TemporalComplianceCheck:
    """
    Result of checking if evidence timestamps are consistent with applicable regulations.
    Used by ForgeryDetector and ScreenshotOCRVerifier.
    """
    obligation_reg_id:   str
    evidence_date:       str
    regulation_date:     str
    is_consistent:       bool
    time_delta_days:     int
    issue:               Optional[str] = None
    notes:               list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "obligation_reg_id": self.obligation_reg_id,
            "evidence_date":     self.evidence_date,
            "regulation_date":   self.regulation_date,
            "is_consistent":     self.is_consistent,
            "time_delta_days":   self.time_delta_days,
            "issue":             self.issue,
            "notes":             self.notes,
        }


# =============================================================================
# Temporal Reasoner
# =============================================================================

class TemporalReasoner:
    """
    Date-aware regulatory validity engine backed by the ComplianceOntology.

    All date arguments accept `date`, `datetime`, or ISO 8601 string.
    """

    def __init__(self, ontology: Optional[ComplianceOntology] = None):
        self._onto = ontology or get_ontology()

    # ── Core Validity ─────────────────────────────────────────────────────────

    def is_current(
        self, node_id_or_reg_id: str, as_of: Optional[date] = None
    ) -> bool:
        """
        Return True if the regulation is in force on `as_of` date.
        Defaults to today if as_of is not provided.
        """
        check_date = self._resolve_date(as_of)
        node = (
            self._onto.get_node(node_id_or_reg_id)
            or self._onto.get_node_by_reg_id(node_id_or_reg_id)
        )
        if not node:
            logger.warning("is_current: node '%s' not found", node_id_or_reg_id)
            return False
        return node.is_active(check_date)

    def get_obligation_validity(
        self, node_id_or_reg_id: str, as_of: Optional[date] = None
    ) -> ObligationValidity:
        """
        Full validity assessment for a regulation at a specific date.
        Includes supersession chain information.
        """
        check_date = self._resolve_date(as_of)
        node = (
            self._onto.get_node(node_id_or_reg_id)
            or self._onto.get_node_by_reg_id(node_id_or_reg_id)
        )

        if not node:
            return ObligationValidity(
                node_id         = node_id_or_reg_id,
                reg_id          = node_id_or_reg_id,
                title           = "Unknown",
                check_date      = check_date.isoformat(),
                is_valid        = False,
                effective_from  = "",
                effective_until = None,
                notes           = [f"Node '{node_id_or_reg_id}' not found in ontology"],
            )

        is_valid = node.is_active(check_date)
        superseded_by = self._onto.get_superseded_by(node.id)
        supersedes    = self._onto.get_supersedes(node.id)

        notes: list[str] = []
        if not is_valid:
            if check_date < node.effective_date:
                notes.append(
                    f"Not yet in force on {check_date.isoformat()} "
                    f"(effective from {node.effective_date.isoformat()})"
                )
            elif node.expiry_date and check_date > node.expiry_date:
                notes.append(
                    f"Expired on {node.expiry_date.isoformat()} — no longer applicable"
                )

        # Check if superseded nodes were also active on check_date
        active_supersessors = [
            n for n in superseded_by if n.is_active(check_date)
        ]
        if active_supersessors:
            notes.append(
                f"Superseded by active regulation(s): "
                + ", ".join(n.reg_id for n in active_supersessors)
            )
            is_valid = False  # Superseded = invalid even if date range overlaps

        return ObligationValidity(
            node_id         = node.id,
            reg_id          = node.reg_id,
            title           = node.title,
            check_date      = check_date.isoformat(),
            is_valid        = is_valid,
            effective_from  = node.effective_date.isoformat(),
            effective_until = node.expiry_date.isoformat() if node.expiry_date else None,
            superseded_by   = [{"id": n.id, "reg_id": n.reg_id, "title": n.title,
                                 "effective_date": n.effective_date.isoformat()}
                                for n in superseded_by],
            supersedes      = [{"id": n.id, "reg_id": n.reg_id, "title": n.title,
                                 "expiry_date": n.expiry_date.isoformat() if n.expiry_date else None}
                                for n in supersedes],
            notes           = notes,
        )

    # ── Effective Obligations ─────────────────────────────────────────────────

    def get_effective_obligations(
        self,
        source: Optional[str] = None,
        domain: Optional[str] = None,
        as_of: Optional[date] = None,
        mandatory_only: bool = False,
    ) -> list[dict[str, Any]]:
        """
        Return all obligations that were in force on `as_of` date.
        Optionally filtered by regulatory source and domain.

        Used by CriticAgent to build its context window.
        """
        check_date = self._resolve_date(as_of)
        all_nodes = self._onto.get_all_nodes(active_only=True, as_of=check_date)

        if source:
            all_nodes = [n for n in all_nodes if n.source.upper() == source.upper()]
        if domain:
            all_nodes = [n for n in all_nodes if n.domain == domain]
        if mandatory_only:
            all_nodes = [n for n in all_nodes if n.mandatory]

        return [n.to_dict() for n in all_nodes]

    # ── Supersession History ──────────────────────────────────────────────────

    def find_superseded(self, node_id_or_reg_id: str) -> list[dict[str, Any]]:
        """
        Return the full chain of obligations that the given regulation supersedes
        (i.e. older regulations displaced by this one).
        """
        node = (
            self._onto.get_node(node_id_or_reg_id)
            or self._onto.get_node_by_reg_id(node_id_or_reg_id)
        )
        if not node:
            return []

        visited: set[str] = set()
        result: list[RegNode] = []

        def _collect_superseded(nid: str) -> None:
            if nid in visited:
                return
            visited.add(nid)
            older = self._onto.get_supersedes(nid)
            for old_node in older:
                result.append(old_node)
                _collect_superseded(old_node.id)

        _collect_superseded(node.id)
        return [n.to_dict() for n in result]

    # ── Timeline Reconstruction ───────────────────────────────────────────────

    def build_regulatory_timeline(
        self,
        domain: str,
        source: Optional[str] = None,
        lookback_days: int = 1825,   # 5 years by default
    ) -> RegulatoryTimeline:
        """
        Build a chronological timeline of regulatory changes in a domain.
        Each entry describes a new regulation, supersession event, or expiry.

        Used by DeepReasoningAgent to explain the regulatory history of an obligation.
        """
        end_date   = date.today()
        start_date = end_date - timedelta(days=lookback_days)

        events: list[SupersessionEvent] = []
        domain_nodes = self._onto.get_nodes_by_domain(domain)

        if source:
            domain_nodes = [n for n in domain_nodes if n.source.upper() == source.upper()]

        for node in domain_nodes:
            # Restrict to our lookback window
            if node.effective_date < start_date:
                continue

            events.append(SupersessionEvent(
                event_date = node.effective_date.isoformat(),
                event_type = "NEW_REGULATION",
                old_reg_id = None,
                new_reg_id = node.reg_id,
                old_title  = None,
                new_title  = node.title,
                impact     = node.summary,
                domain     = node.domain,
                source     = node.source,
            ))

            # Supersession events from this node
            for older_node in self._onto.get_supersedes(node.id):
                # Get the edge to find its effective date
                edges = self._onto.get_edges(
                    source_id  = node.id,
                    target_id  = older_node.id,
                    edge_type  = EdgeType.SUPERSEDES,
                )
                for edge in edges:
                    if edge.effective_date >= start_date:
                        events.append(SupersessionEvent(
                            event_date = edge.effective_date.isoformat(),
                            event_type = "SUPERSESSION",
                            old_reg_id = older_node.reg_id,
                            new_reg_id = node.reg_id,
                            old_title  = older_node.title,
                            new_title  = node.title,
                            impact     = edge.notes or f"{node.reg_id} supersedes {older_node.reg_id}",
                            domain     = domain,
                            source     = node.source,
                        ))

            # Expiry events
            if node.expiry_date and start_date <= node.expiry_date <= end_date:
                events.append(SupersessionEvent(
                    event_date = node.expiry_date.isoformat(),
                    event_type = "EXPIRY",
                    old_reg_id = node.reg_id,
                    new_reg_id = None,
                    old_title  = node.title,
                    new_title  = None,
                    impact     = f"{node.reg_id} expired — no longer applicable",
                    domain     = node.domain,
                    source     = node.source,
                ))

        # Sort chronologically
        events.sort(key=lambda e: e.event_date)

        return RegulatoryTimeline(
            domain       = domain,
            source       = source,
            start_date   = start_date.isoformat(),
            end_date     = end_date.isoformat(),
            events       = [e.to_dict() for e in events],
            change_count = len(events),
        )

    # ── Evidence Timestamp Validation ─────────────────────────────────────────

    def check_evidence_timestamp(
        self,
        reg_id_or_node_id: str,
        evidence_date: date,
    ) -> TemporalComplianceCheck:
        """
        Verify that evidence was collected after the regulation's effective date.

        Example use case:
          A bank submits an "AES-256 configuration screenshot" dated 2023-02-01
          as evidence of compliance with RBI IT Framework 2023 (effective 2024-04-01).
          → The evidence pre-dates the regulation — temporally inconsistent.

        Used by ForgeryDetector in the validation layer.
        """
        node = (
            self._onto.get_node(reg_id_or_node_id)
            or self._onto.get_node_by_reg_id(reg_id_or_node_id)
        )

        if not node:
            return TemporalComplianceCheck(
                obligation_reg_id  = reg_id_or_node_id,
                evidence_date      = evidence_date.isoformat(),
                regulation_date    = "",
                is_consistent      = False,
                time_delta_days    = 0,
                issue              = f"Regulation '{reg_id_or_node_id}' not found",
                notes              = ["Cannot verify temporal consistency — regulation unknown"],
            )

        delta = (evidence_date - node.effective_date).days
        is_consistent = delta >= 0
        issue = None
        notes: list[str] = []

        if not is_consistent:
            issue = (
                f"Evidence dated {evidence_date.isoformat()} pre-dates regulation "
                f"effective date {node.effective_date.isoformat()} "
                f"by {abs(delta)} days."
            )
            notes.append(
                f"RED FLAG: Evidence collected before regulation existed. "
                f"This suggests the evidence may have been backdated or is for the wrong obligation."
            )
        else:
            # Evidence is after effective date — check if reg was still active
            if node.expiry_date and evidence_date > node.expiry_date:
                notes.append(
                    f"WARNING: Regulation expired {node.expiry_date.isoformat()} "
                    f"but evidence is dated {evidence_date.isoformat()}. "
                    f"Evidence may be for an outdated standard."
                )
            else:
                notes.append(
                    f"Evidence date {evidence_date.isoformat()} is {delta} days "
                    f"after regulation effective date — temporally consistent."
                )

        return TemporalComplianceCheck(
            obligation_reg_id  = node.reg_id,
            evidence_date      = evidence_date.isoformat(),
            regulation_date    = node.effective_date.isoformat(),
            is_consistent      = is_consistent,
            time_delta_days    = delta,
            issue              = issue,
            notes              = notes,
        )

    # ── Recent Changes ────────────────────────────────────────────────────────

    def get_recent_changes(self, lookback_days: int = 90) -> list[dict[str, Any]]:
        """
        Return regulations that became effective or expired in the last N days.
        Useful for: generating a "what's changed recently" digest.
        """
        cutoff = date.today() - timedelta(days=lookback_days)
        changes: list[dict[str, Any]] = []

        for node in self._onto.get_all_nodes():
            if node.effective_date >= cutoff:
                changes.append({
                    "type":           "NEW_OR_AMENDED",
                    "reg_id":         node.reg_id,
                    "title":          node.title,
                    "source":         node.source,
                    "domain":         node.domain,
                    "effective_date": node.effective_date.isoformat(),
                    "mandatory":      node.mandatory,
                })
            if node.expiry_date and node.expiry_date >= cutoff:
                changes.append({
                    "type":        "EXPIRED",
                    "reg_id":      node.reg_id,
                    "title":       node.title,
                    "source":      node.source,
                    "domain":      node.domain,
                    "expiry_date": node.expiry_date.isoformat(),
                })

        changes.sort(key=lambda x: x.get("effective_date", x.get("expiry_date", "")),
                     reverse=True)
        return changes

    def get_encryption_standard_history(self) -> list[dict[str, Any]]:
        """
        Specific query: timeline of encryption standard changes (AES-128 → AES-256).
        Used by DeepReasoningAgent when processing encryption obligations.
        """
        encryption_nodes = self._onto.get_nodes_by_domain("encryption")
        cyber_nodes      = self._onto.get_nodes_by_domain("cyber")
        all_relevant     = encryption_nodes + cyber_nodes

        history: list[dict[str, Any]] = []
        for node in sorted(all_relevant, key=lambda n: n.effective_date):
            # Only include nodes with encryption-related clauses
            enc_clauses = [
                c for c in node.clauses
                if any(kw in c.lower() for kw in ["encrypt", "aes", "tls", "ssl"])
            ]
            if enc_clauses:
                history.append({
                    "reg_id":         node.reg_id,
                    "title":          node.title,
                    "source":         node.source,
                    "effective_date": node.effective_date.isoformat(),
                    "expiry_date":    node.expiry_date.isoformat() if node.expiry_date else None,
                    "encryption_clauses": enc_clauses,
                    "is_active":      node.is_active(),
                })

        return history

    # ── Utility ───────────────────────────────────────────────────────────────

    @staticmethod
    def _resolve_date(d: Optional[Any]) -> date:
        """Accept date, datetime, ISO string, or None → return date."""
        if d is None:
            return date.today()
        if isinstance(d, datetime):
            return d.date()
        if isinstance(d, date):
            return d
        if isinstance(d, str):
            return date.fromisoformat(d[:10])
        raise TypeError(f"Cannot resolve date from {type(d)}: {d}")
