"""
audit/logger.py
===============
Immutable Audit Logger with blockchain-style hash chaining and WORM storage.

Architecture
------------
  ┌────────────────────────────────────────────────┐
  │               AuditLogger                      │
  │                                                │
  │  ┌──────────────────────────────────────────┐  │
  │  │  Chain Manager                           │  │
  │  │  • new_chain() → chain_id                │  │
  │  │  • append(chain_id, node_type, payload)  │  │
  │  │  • finalize(chain_id) → reports          │  │
  │  └──────────────────────────────────────────┘  │
  │  ┌──────────────────────────────────────────┐  │
  │  │  Hash Engine (blockchain-style)          │  │
  │  │  • SHA-256 HMAC of (prev_hash + payload) │  │
  │  │  • Immutable append-only chain           │  │
  │  └──────────────────────────────────────────┘  │
  │  ┌──────────────────────────────────────────┐  │
  │  │  WORM Storage (Cosmos DB + ADLS)         │  │
  │  │  • Append-only, no update/delete         │  │
  │  │  • Tamper detection via chain verify     │  │
  │  └──────────────────────────────────────────┘  │
  │  ┌──────────────────────────────────────────┐  │
  │  │  Report Generator                        │  │
  │  │  • XBRL output                           │  │
  │  │  • Signed PDF generation                 │  │
  │  └──────────────────────────────────────────┘  │
  └────────────────────────────────────────────────┘

Each node in an audit chain stores:
  - node_index:   sequential position in the chain
  - node_type:    REGULATION | MAP | TASK | EVIDENCE | SIGNOFF
  - payload:      JSON-serialisable dict of stage output
  - prev_hash:    SHA-256 HMAC of the previous node (genesis = "0" * 64)
  - current_hash: SHA-256 HMAC of (prev_hash + canonical payload)
  - timestamp:    ISO 8601 in UTC
  - actor:        system identity or human who created the node
  - explainability: optional metadata for AI decision transparency

Dependencies
------------
  pip install azure-cosmos azure-identity
  Optional: reportlab (PDF signing), lxml (XBRL)
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import smtplib
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.message import EmailMessage
from enum import Enum
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────────────────────
GENESIS_HASH = "0" * 64  # SHA-256 zero hash for the first node in a chain


# ──────────────────────────────────────────────────────────────────────────────
# Enumerations
# ──────────────────────────────────────────────────────────────────────────────

class NodeType(str, Enum):
    REGULATION = "REGULATION"
    MAP        = "MAP"
    TASK       = "TASK"
    EVIDENCE   = "EVIDENCE"
    SIGNOFF    = "SIGNOFF"


# ──────────────────────────────────────────────────────────────────────────────
# Data Structures
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class ExplainabilityMetadata:
    """Captures AI decision transparency for regulatory audit."""
    decision_id:           str
    model_version:         str
    feature_weights:       dict[str, float]
    confidence_score:      float
    reasoning_steps:       list[str]
    alternative_decisions: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision_id":           self.decision_id,
            "model_version":         self.model_version,
            "feature_weights":       self.feature_weights,
            "confidence_score":      round(self.confidence_score, 6),
            "reasoning_steps":       self.reasoning_steps,
            "alternative_decisions": self.alternative_decisions,
        }


@dataclass
class AuditNode:
    """Single immutable node in an audit chain."""
    chain_id:        str
    node_index:      int
    node_type:       NodeType
    payload:         dict[str, Any]
    prev_hash:       str
    current_hash:    str
    timestamp:       str
    actor:           str           = "system"
    explainability:  Optional[ExplainabilityMetadata] = None

    def to_dict(self) -> dict[str, Any]:
        d = {
            "chain_id":     self.chain_id,
            "node_index":   self.node_index,
            "node_type":    self.node_type.value,
            "payload":      self.payload,
            "prev_hash":    self.prev_hash,
            "current_hash": self.current_hash,
            "timestamp":    self.timestamp,
            "actor":        self.actor,
        }
        if self.explainability:
            d["explainability"] = self.explainability.to_dict()
        return d


@dataclass
class AuditChain:
    """Ordered sequence of AuditNodes forming an immutable hash chain."""
    chain_id:   str
    nodes:      list[AuditNode] = field(default_factory=list)
    finalized:  bool = False

    @property
    def head_hash(self) -> str:
        """Return the hash of the last node, or GENESIS_HASH if empty."""
        return self.nodes[-1].current_hash if self.nodes else GENESIS_HASH

    @property
    def length(self) -> int:
        return len(self.nodes)


# ──────────────────────────────────────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class AuditLoggerConfig:
    """Configuration for the AuditLogger."""
    # Cosmos DB (WORM storage backend)
    cosmos_connection_string: Optional[str] = None
    cosmos_database:          str = "compliancedb"
    cosmos_container:         str = "audit_ledger"

    # HMAC secret for hash computation
    hmac_secret:              Optional[bytes] = None

    # PDF signing key (PEM-encoded RSA private key)
    private_key_pem:          Optional[bytes] = None

    # Override actors allowed to append SIGNOFF nodes
    approved_override_actors: set[str] = field(default_factory=set)

    # Alert configuration
    smtp_host:         Optional[str] = None
    smtp_port:         int = 587
    alert_sender:      Optional[str] = None
    alert_recipients:  list[str] = field(default_factory=list)


# ──────────────────────────────────────────────────────────────────────────────
# Hash Engine
# ──────────────────────────────────────────────────────────────────────────────

class HashEngine:
    """
    Computes blockchain-style hashes for audit nodes.

    Each node's hash is:
        HMAC-SHA256(key=hmac_secret, msg=prev_hash + canonical_payload_json)

    If no HMAC secret is provided, falls back to plain SHA-256.
    """

    def __init__(self, hmac_secret: Optional[bytes] = None):
        self._secret = hmac_secret

    def compute_hash(self, prev_hash: str, payload: dict[str, Any]) -> str:
        """Compute the hash for a new node given the previous hash and payload."""
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                               ensure_ascii=False, default=str)
        message = f"{prev_hash}{canonical}".encode("utf-8")

        if self._secret:
            return hmac.new(self._secret, message, hashlib.sha256).hexdigest()
        else:
            return hashlib.sha256(message).hexdigest()

    def verify_chain(self, nodes: list[AuditNode]) -> tuple[bool, list[str]]:
        """
        Verify the integrity of an entire audit chain.

        Returns (is_valid, list_of_errors).
        """
        errors: list[str] = []
        if not nodes:
            return True, []

        # First node must reference GENESIS_HASH
        if nodes[0].prev_hash != GENESIS_HASH:
            errors.append(
                f"Node 0: prev_hash should be GENESIS ({GENESIS_HASH[:16]}…) "
                f"but got {nodes[0].prev_hash[:16]}…"
            )

        for i, node in enumerate(nodes):
            expected = self.compute_hash(node.prev_hash, node.payload)
            if node.current_hash != expected:
                errors.append(
                    f"Node {i}: hash mismatch. "
                    f"Expected {expected[:16]}…, got {node.current_hash[:16]}…"
                )

            # Chain linkage: node[i].prev_hash must equal node[i-1].current_hash
            if i > 0 and node.prev_hash != nodes[i - 1].current_hash:
                errors.append(
                    f"Node {i}: prev_hash does not match node {i-1} current_hash. "
                    f"Chain is broken."
                )

        return len(errors) == 0, errors


# ──────────────────────────────────────────────────────────────────────────────
# WORM Storage Backend
# ──────────────────────────────────────────────────────────────────────────────

class WORMStorage:
    """
    Write-Once-Read-Many storage backend.

    Uses Azure Cosmos DB with append-only semantics.
    Documents are written with a unique id (chain_id + node_index) and
    never updated or deleted.

    Falls back to in-memory storage if Cosmos DB is not configured.
    """

    def __init__(self, connection_string: Optional[str] = None,
                 database: str = "compliancedb",
                 container: str = "audit_ledger"):
        self._cosmos_client = None
        self._container = None
        self._in_memory: dict[str, list[dict[str, Any]]] = {}

        if connection_string:
            try:
                from azure.cosmos import CosmosClient, PartitionKey
                self._cosmos_client = CosmosClient.from_connection_string(
                    connection_string
                )
                db = self._cosmos_client.create_database_if_not_exists(database)
                self._container = db.create_container_if_not_exists(
                    id=container,
                    partition_key=PartitionKey(path="/chain_id"),
                    default_ttl=-1,  # no expiry — WORM
                )
                logger.info("WORM storage connected to Cosmos DB: %s/%s",
                            database, container)
            except Exception as exc:
                logger.warning(
                    "Cosmos DB connection failed (%s). Using in-memory WORM storage. "
                    "This is NOT suitable for production.", exc
                )
                self._container = None

    def write(self, node: AuditNode) -> None:
        """Append a node to WORM storage. No update or delete is permitted."""
        doc = node.to_dict()
        doc["id"] = f"{node.chain_id}_{node.node_index:06d}"

        if self._container:
            try:
                self._container.create_item(body=doc)
                logger.debug("WORM write: chain=%s node=%d",
                             node.chain_id, node.node_index)
            except Exception as exc:
                logger.error("WORM write failed: %s. Storing in-memory.", exc)
                self._write_in_memory(node.chain_id, doc)
        else:
            self._write_in_memory(node.chain_id, doc)

    def read_chain(self, chain_id: str) -> list[dict[str, Any]]:
        """Read all nodes for a chain, ordered by node_index."""
        if self._container:
            try:
                query = (
                    "SELECT * FROM c WHERE c.chain_id = @cid "
                    "ORDER BY c.node_index ASC"
                )
                items = list(self._container.query_items(
                    query=query,
                    parameters=[{"name": "@cid", "value": chain_id}],
                    enable_cross_partition_query=False,
                ))
                return items
            except Exception as exc:
                logger.error("WORM read failed: %s. Reading from in-memory.", exc)

        return self._in_memory.get(chain_id, [])

    def _write_in_memory(self, chain_id: str, doc: dict[str, Any]) -> None:
        if chain_id not in self._in_memory:
            self._in_memory[chain_id] = []
        self._in_memory[chain_id].append(doc)


# ──────────────────────────────────────────────────────────────────────────────
# Report Generator
# ──────────────────────────────────────────────────────────────────────────────

class ReportGenerator:
    """Generates XBRL and signed PDF audit reports from a finalised chain."""

    def __init__(self, private_key_pem: Optional[bytes] = None):
        self._private_key_pem = private_key_pem

    def generate_xbrl(self, chain: AuditChain, output_dir: Path) -> Path:
        """Generate an XBRL-formatted report for the audit chain."""
        output_dir.mkdir(parents=True, exist_ok=True)
        xbrl_path = output_dir / f"{chain.chain_id}_audit.xbrl"

        xbrl_content = self._build_xbrl_xml(chain)
        xbrl_path.write_text(xbrl_content, encoding="utf-8")
        logger.info("XBRL report generated: %s", xbrl_path)
        return xbrl_path

    def generate_signed_pdf(self, chain: AuditChain, output_dir: Path) -> Path:
        """Generate a signed PDF audit trail report."""
        output_dir.mkdir(parents=True, exist_ok=True)
        pdf_path = output_dir / f"{chain.chain_id}_audit.pdf"

        pdf_content = self._build_pdf_content(chain)

        # Sign the content if a private key is available
        signature = ""
        if self._private_key_pem:
            signature = self._compute_signature(pdf_content)

        # Write PDF (simplified — in production use reportlab or similar)
        full_content = pdf_content
        if signature:
            full_content += f"\n\n--- DIGITAL SIGNATURE ---\n{signature}\n"

        pdf_path.write_text(full_content, encoding="utf-8")
        logger.info("Signed PDF report generated: %s", pdf_path)
        return pdf_path

    def _build_xbrl_xml(self, chain: AuditChain) -> str:
        """Build XBRL XML content from the audit chain."""
        nodes_xml = ""
        for node in chain.nodes:
            nodes_xml += (
                f'  <audit:node index="{node.node_index}" '
                f'type="{node.node_type.value}">\n'
                f'    <audit:hash>{node.current_hash}</audit:hash>\n'
                f'    <audit:prevHash>{node.prev_hash}</audit:prevHash>\n'
                f'    <audit:timestamp>{node.timestamp}</audit:timestamp>\n'
                f'    <audit:actor>{node.actor}</audit:actor>\n'
                f'    <audit:payload>{json.dumps(node.payload, default=str)}'
                f'</audit:payload>\n'
                f'  </audit:node>\n'
            )

        return (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<xbrl xmlns="http://www.xbrl.org/2003/instance"\n'
            '      xmlns:audit="urn:compliance:audit:1.0">\n'
            f'  <audit:chainId>{chain.chain_id}</audit:chainId>\n'
            f'  <audit:nodeCount>{chain.length}</audit:nodeCount>\n'
            f'  <audit:headHash>{chain.head_hash}</audit:headHash>\n'
            f'  <audit:finalized>{chain.finalized}</audit:finalized>\n'
            f'{nodes_xml}'
            '</xbrl>\n'
        )

    def _build_pdf_content(self, chain: AuditChain) -> str:
        """Build text content for the PDF report."""
        lines = [
            "=" * 72,
            "COMPLIANCE AUDIT TRAIL REPORT",
            "=" * 72,
            f"Chain ID   : {chain.chain_id}",
            f"Nodes      : {chain.length}",
            f"Head Hash  : {chain.head_hash}",
            f"Finalized  : {chain.finalized}",
            f"Generated  : {datetime.now(tz=timezone.utc).isoformat()}",
            "-" * 72,
        ]

        for node in chain.nodes:
            lines.extend([
                f"\n[Node {node.node_index}] {node.node_type.value}",
                f"  Timestamp : {node.timestamp}",
                f"  Actor     : {node.actor}",
                f"  Prev Hash : {node.prev_hash[:32]}…",
                f"  Hash      : {node.current_hash[:32]}…",
                f"  Payload   : {json.dumps(node.payload, indent=4, default=str)}",
            ])
            if node.explainability:
                lines.append(
                    f"  AI Explain: confidence={node.explainability.confidence_score:.4f}, "
                    f"model={node.explainability.model_version}"
                )

        lines.append("\n" + "=" * 72)
        lines.append("END OF REPORT")
        return "\n".join(lines)

    def _compute_signature(self, content: str) -> str:
        """Compute an HMAC-SHA256 signature of the report content."""
        if not self._private_key_pem:
            return ""
        return hmac.new(
            self._private_key_pem, content.encode("utf-8"), hashlib.sha256
        ).hexdigest()


# ──────────────────────────────────────────────────────────────────────────────
# Alert Service
# ──────────────────────────────────────────────────────────────────────────────

class AlertService:
    """Sends email alerts for tamper detection and compliance violations."""

    def __init__(self, smtp_host: Optional[str] = None, smtp_port: int = 587,
                 sender: Optional[str] = None,
                 recipients: Optional[list[str]] = None):
        self._smtp_host = smtp_host
        self._smtp_port = smtp_port
        self._sender = sender
        self._recipients = recipients or []

    def send_tamper_alert(self, chain_id: str, errors: list[str]) -> bool:
        """Send an alert when tamper detection finds chain integrity violations."""
        if not self._smtp_host or not self._sender or not self._recipients:
            logger.warning(
                "Alert not sent (SMTP not configured). Chain=%s, Errors=%s",
                chain_id, errors
            )
            return False

        subject = f"[CRITICAL] Audit Chain Tamper Detected — {chain_id}"
        body = (
            f"Audit chain tamper detected.\n\n"
            f"Chain ID: {chain_id}\n"
            f"Errors:\n" + "\n".join(f"  • {e}" for e in errors) + "\n\n"
            f"Immediate investigation required.\n"
            f"Timestamp: {datetime.now(tz=timezone.utc).isoformat()}"
        )

        return self._send_email(subject, body)

    def _send_email(self, subject: str, body: str) -> bool:
        """Send an email via SMTP with TLS."""
        try:
            msg = EmailMessage()
            msg["Subject"] = subject
            msg["From"] = self._sender
            msg["To"] = ", ".join(self._recipients)
            msg.set_content(body)

            with smtplib.SMTP(self._smtp_host, self._smtp_port) as server:
                server.starttls()
                server.send_message(msg)

            logger.info("Alert email sent: %s", subject)
            return True
        except Exception as exc:
            logger.error("Failed to send alert email: %s", exc)
            return False


# ──────────────────────────────────────────────────────────────────────────────
# AuditLogger — Main Facade
# ──────────────────────────────────────────────────────────────────────────────

class AuditLogger:
    """
    Central audit logging facade.

    Provides:
      - new_chain()  → create a new audit chain
      - append()     → add a node with blockchain-style hashing
      - finalize()   → close the chain, run tamper detection, generate reports
      - verify()     → verify chain integrity at any time

    Every append creates an immutable hash referencing the previous node's hash.
    """

    def __init__(self, config: AuditLoggerConfig):
        self._config = config
        self._hash_engine = HashEngine(config.hmac_secret)
        self._worm = WORMStorage(
            connection_string=config.cosmos_connection_string,
            database=config.cosmos_database,
            container=config.cosmos_container,
        )
        self._report_gen = ReportGenerator(config.private_key_pem)
        self._alert_svc = AlertService(
            smtp_host=config.smtp_host,
            smtp_port=config.smtp_port,
            sender=config.alert_sender,
            recipients=config.alert_recipients,
        )
        # In-memory chain registry (for active chains being built)
        self._chains: dict[str, AuditChain] = {}

        logger.info("AuditLogger initialised (HMAC=%s, Cosmos=%s)",
                     "enabled" if config.hmac_secret else "disabled",
                     "connected" if config.cosmos_connection_string else "in-memory")

    # ── Public API ────────────────────────────────────────────────────────────

    def new_chain(self) -> str:
        """Create a new audit chain and return its ID."""
        chain_id = str(uuid.uuid4())
        self._chains[chain_id] = AuditChain(chain_id=chain_id)
        logger.info("Audit chain created: %s", chain_id)
        return chain_id

    def append(
        self,
        chain_id: str,
        node_type: NodeType,
        payload: dict[str, Any],
        *,
        actor: str = "system",
        explainability: Optional[ExplainabilityMetadata] = None,
    ) -> AuditNode:
        """
        Append a new node to the chain with blockchain-style hash linking.

        Each node's hash = HMAC-SHA256(prev_hash + canonical_payload).
        This creates an immutable, tamper-evident chain.

        Raises
        ------
        ValueError   If the chain doesn't exist or is already finalized.
        PermissionError  If a SIGNOFF node is attempted by an unapproved actor.
        """
        chain = self._chains.get(chain_id)
        if chain is None:
            raise ValueError(f"Chain '{chain_id}' does not exist.")
        if chain.finalized:
            raise ValueError(f"Chain '{chain_id}' is already finalized. "
                             "Cannot append new nodes.")

        # Only approved actors can append SIGNOFF nodes
        if node_type == NodeType.SIGNOFF:
            if actor not in self._config.approved_override_actors:
                raise PermissionError(
                    f"Actor '{actor}' is not approved to append SIGNOFF nodes. "
                    f"Approved actors: {self._config.approved_override_actors}"
                )

        prev_hash = chain.head_hash
        current_hash = self._hash_engine.compute_hash(prev_hash, payload)
        timestamp = datetime.now(tz=timezone.utc).isoformat()

        node = AuditNode(
            chain_id=chain_id,
            node_index=chain.length,
            node_type=node_type,
            payload=payload,
            prev_hash=prev_hash,
            current_hash=current_hash,
            timestamp=timestamp,
            actor=actor,
            explainability=explainability,
        )

        chain.nodes.append(node)

        # Persist to WORM storage immediately
        self._worm.write(node)

        logger.debug("Audit node appended: chain=%s index=%d type=%s hash=%s",
                      chain_id, node.node_index, node_type.value,
                      current_hash[:16])
        return node

    def finalize(self, chain_id: str, output_dir: Path) -> dict[str, Path]:
        """
        Finalize the audit chain:
          1. Run tamper detection (verify all hashes)
          2. Generate XBRL report
          3. Generate signed PDF report
          4. Mark chain as finalized (no further appends)
          5. Alert if tamper is detected

        Returns a dict of report type → file path.
        """
        chain = self._chains.get(chain_id)
        if chain is None:
            raise ValueError(f"Chain '{chain_id}' does not exist.")

        # Step 1: Tamper detection
        is_valid, errors = self.verify(chain_id)
        if not is_valid:
            logger.critical("TAMPER DETECTED in chain %s: %s", chain_id, errors)
            self._alert_svc.send_tamper_alert(chain_id, errors)

        # Step 2 & 3: Generate reports
        reports: dict[str, Path] = {}
        try:
            reports["xbrl"] = self._report_gen.generate_xbrl(chain, output_dir)
        except Exception as exc:
            logger.error("XBRL report generation failed: %s", exc)

        try:
            reports["pdf"] = self._report_gen.generate_signed_pdf(chain, output_dir)
        except Exception as exc:
            logger.error("PDF report generation failed: %s", exc)

        # Step 4: Mark as finalized
        chain.finalized = True
        logger.info("Audit chain finalized: %s (nodes=%d, valid=%s)",
                     chain_id, chain.length, is_valid)

        return reports

    def verify(self, chain_id: str) -> tuple[bool, list[str]]:
        """Verify the integrity of the entire audit chain."""
        chain = self._chains.get(chain_id)
        if chain is None:
            raise ValueError(f"Chain '{chain_id}' does not exist.")

        return self._hash_engine.verify_chain(chain.nodes)

    def get_chain(self, chain_id: str) -> Optional[AuditChain]:
        """Return the in-memory chain object (read-only inspection)."""
        return self._chains.get(chain_id)

    def get_traceability_graph(self, chain_id: str) -> dict[str, Any]:
        """
        Build a traceability graph showing the full lineage of an audit chain.

        Returns a JSON-serialisable dict with nodes and edges suitable for
        graph visualisation.
        """
        chain = self._chains.get(chain_id)
        if chain is None:
            raise ValueError(f"Chain '{chain_id}' does not exist.")

        graph_nodes = []
        graph_edges = []

        for node in chain.nodes:
            graph_nodes.append({
                "id":        f"{chain_id}:{node.node_index}",
                "type":      node.node_type.value,
                "hash":      node.current_hash[:16],
                "timestamp": node.timestamp,
                "actor":     node.actor,
            })
            if node.node_index > 0:
                graph_edges.append({
                    "from": f"{chain_id}:{node.node_index - 1}",
                    "to":   f"{chain_id}:{node.node_index}",
                    "label": f"hash_link ({node.prev_hash[:8]}…)",
                })

        return {
            "chain_id":  chain_id,
            "finalized": chain.finalized,
            "nodes":     graph_nodes,
            "edges":     graph_edges,
        }
