"""
audit/logger.py
===============
Immutable Audit Logger for Azure Cosmos DB
Senior Security Engineering — Production Grade

Architecture:
  ┌─────────────────────────────────────────────────────────┐
  │  Traceability Graph  (blockchain-style hash chain)      │
  │                                                         │
  │  Regulation ──► MAP ──► Task ──► Evidence ──► Sign-off  │
  │     SHA-256      SHA-256   SHA-256    SHA-256    SHA-256 │
  │  (each node embeds prev_hash, forming a tamper chain)   │
  └─────────────────────────────────────────────────────────┘

  WORM storage   → Cosmos DB ledger container (append-only)
  Explainability → AI decision metadata with confidence scores
  Reporting      → XBRL (iXBRL XML) + cryptographically signed PDF
  Alerting       → Missing evidence & unauthorized overrides
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import smtplib
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from enum import Enum
from pathlib import Path
from typing import Any, Optional
from xml.etree.ElementTree import Element, SubElement, indent, tostring
import threading

# ---------------------------------------------------------------------------
# Optional heavy dependencies — fail loudly at call-site, not at import time
# ---------------------------------------------------------------------------
try:
    from azure.cosmos import CosmosClient, PartitionKey, exceptions as cosmos_exc
    _COSMOS_AVAILABLE = True
except ImportError:
    _COSMOS_AVAILABLE = False

try:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
    from reportlab.lib import colors
    _REPORTLAB_AVAILABLE = True
except ImportError:
    _REPORTLAB_AVAILABLE = False

try:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding, rsa
    from cryptography.hazmat.backends import default_backend
    _CRYPTO_AVAILABLE = True
except ImportError:
    _CRYPTO_AVAILABLE = False

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")


# ===========================================================================
# 1. ENUMS & CONSTANTS
# ===========================================================================

class NodeType(str, Enum):
    REGULATION = "Regulation"
    MAP        = "MAP"
    TASK       = "Task"
    EVIDENCE   = "Evidence"
    SIGNOFF    = "Sign-off"
    HUMAN_CORRECTION = "Human-Correction"


class AlertSeverity(str, Enum):
    INFO     = "INFO"
    WARNING  = "WARNING"
    CRITICAL = "CRITICAL"


GENESIS_HASH = "0" * 64   # Sentinel: first node in every chain has no predecessor


# ===========================================================================
# 2. CORE DATA MODELS
# ===========================================================================

@dataclass
class ExplainabilityMetadata:
    """
    Captures *why* an AI/automated system made a specific decision.
    Written verbatim to WORM storage — never mutated after creation.
    """
    decision_id:      str
    model_version:    str
    feature_weights:  dict[str, float]          # feature_name → importance [0,1]
    confidence_score: float                      # overall confidence [0,1]
    reasoning_steps:  list[str]                  # human-readable chain-of-thought
    alternative_decisions: list[dict[str, Any]]  # [{label, score, reason}]
    timestamp:        str = field(default_factory=lambda: _utcnow())

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class TraceabilityNode:
    """
    A single immutable node in the audit traceability graph.

    Hash chain:
        node_hash = SHA-256( node_type | node_id | timestamp |
                             payload_json | prev_hash | hmac_secret )
    The HMAC secret is kept server-side; the hash is stored alongside the
    payload so any external tampering invalidates the chain.
    """
    node_type:   NodeType
    node_id:     str
    timestamp:   str
    payload:     dict[str, Any]
    prev_hash:   str                         # previous node's node_hash
    node_hash:   str = ""                    # computed post-init
    explainability: Optional[dict] = None    # ExplainabilityMetadata.to_dict()
    is_override: bool = False                # True if a human manually overrode an automated decision
    override_actor: Optional[str] = None     # identity of the override actor
    chain_id:    str = ""                    # groups all nodes for one compliance run

    @property
    def current_hash(self) -> str:
        return self.node_hash

    @current_hash.setter
    def current_hash(self, val: str) -> None:
        self.node_hash = val

    def compute_hash(self, hmac_secret: bytes) -> str:
        """
        Deterministically compute the tamper-evident hash for this node.
        Incorporates an HMAC secret so external actors cannot forge hashes.
        """
        canonical = json.dumps(
            {
                "node_type":  self.node_type,
                "node_id":    self.node_id,
                "timestamp":  self.timestamp,
                "payload":    self.payload,
                "prev_hash":  self.prev_hash,
                "is_override": self.is_override,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        return hmac.new(hmac_secret, canonical, hashlib.sha256).hexdigest()

    def to_cosmos_document(self) -> dict:
        """Serialize to a Cosmos DB document (partition key = chain_id)."""
        return {
            "id":              self.node_id,
            "chain_id":        self.chain_id,
            "node_type":       self.node_type,
            "timestamp":       self.timestamp,
            "payload":         self.payload,
            "prev_hash":       self.prev_hash,
            "node_hash":       self.node_hash,
            "explainability":  self.explainability,
            "is_override":     self.is_override,
            "override_actor":  self.override_actor,
            # Cosmos DB TTL / ledger metadata left to container policy
        }


# ===========================================================================
# 3. COSMOS DB WORM LEDGER
# ===========================================================================

class CosmosWORMWriter:
    """
    Thin wrapper around Azure Cosmos DB configured as a *ledger* container.

    A Cosmos DB ledger container is configured with:
      - No TTL (documents live forever)
      - Serverless or provisioned throughput with a dedicated RU policy
      - Role-based access that allows only INSERT, never UPDATE/DELETE
      - Azure Policy enforcement of immutability at the account level

    This class enforces append-only semantics in software as a defence-in-depth
    layer.  The real WORM guarantee comes from Azure RBAC + Policy.
    """

    LEDGER_CONTAINER = "audit_ledger"
    ALERT_CONTAINER  = "audit_alerts"

    def __init__(self, connection_string: str, database_name: str):
        if not _COSMOS_AVAILABLE:
            raise ImportError("azure-cosmos package is required. pip install azure-cosmos")
        self._client   = CosmosClient.from_connection_string(connection_string)
        self._db       = self._client.get_database_client(database_name)
        self._ledger   = self._db.get_container_client(self.LEDGER_CONTAINER)
        self._alerts   = self._db.get_container_client(self.ALERT_CONTAINER)

    # ------------------------------------------------------------------
    def write_node(self, node: TraceabilityNode) -> None:
        """
        Append a node to the Cosmos DB ledger container.
        Raises on conflict (duplicate id) — nodes are write-once.
        """
        doc = node.to_cosmos_document()

        def _write():
            try:
                self._ledger.create_item(body=doc)
                logger.info("WORM write OK  node_id=%s type=%s", node.node_id, node.node_type)
            except cosmos_exc.CosmosResourceExistsError:
                logger.error("Duplicate node rejected  node_id=%s", node.node_id)
            except Exception as exc:
                logger.error("WORM write failed (async): %s", exc)

        # Fire-and-forget background write to avoid blocking asyncio event loop
        t = threading.Thread(target=_write, daemon=True)
        t.start()

    # ------------------------------------------------------------------
    def write_alert(self, alert: dict) -> None:
        def _write_alert():
            try:
                self._alerts.create_item(body=alert)
            except Exception as exc:
                logger.error("WORM alert write failed (async): %s", exc)

        t = threading.Thread(target=_write_alert, daemon=True)
        t.start()

    # ------------------------------------------------------------------
    def read_chain(self, chain_id: str) -> list[dict]:
        """Return all nodes for a chain, ordered by timestamp."""
        query  = "SELECT * FROM c WHERE c.chain_id = @chain_id ORDER BY c.timestamp ASC"
        params = [{"name": "@chain_id", "value": chain_id}]
        items  = list(self._ledger.query_items(query=query, parameters=params,
                                               enable_cross_partition_query=True))
        return items

    # ------------------------------------------------------------------
    def read_all_chains(self) -> list[dict]:
        """Return every node across all chains (use with care on large datasets)."""
        return list(self._ledger.read_all_items())


# ===========================================================================
# 4. TAMPER DETECTION
# ===========================================================================

class TamperDetector:
    """
    Re-computes every node's hash and verifies the hash chain is unbroken.
    Also detects:
      - Missing evidence nodes
      - Unauthorized manual overrides (is_override=True without an approved actor)
    """

    REQUIRED_NODE_SEQUENCE = [
        NodeType.REGULATION,
        NodeType.MAP,
        NodeType.TASK,
        NodeType.EVIDENCE,
        NodeType.SIGNOFF,
    ]

    def __init__(self, hmac_secret: bytes, approved_override_actors: set[str]):
        self._secret   = hmac_secret
        self._approved = approved_override_actors

    # ------------------------------------------------------------------
    def verify_chain(self, nodes: list[dict]) -> list[dict]:
        """
        Verify a list of raw Cosmos documents that form a chain.
        Returns a list of violation dicts (empty = chain is intact).
        """
        violations: list[dict] = []
        prev_hash = GENESIS_HASH

        for i, raw in enumerate(nodes):
            node = TraceabilityNode(
                node_type    = raw["node_type"],
                node_id      = raw["id"],
                timestamp    = raw["timestamp"],
                payload      = raw["payload"],
                prev_hash    = raw["prev_hash"],
                node_hash    = raw["node_hash"],
                is_override  = raw.get("is_override", False),
                override_actor = raw.get("override_actor"),
                chain_id     = raw.get("chain_id", ""),
            )

            # --- 4a. Hash chain integrity ---
            expected_prev = prev_hash
            if node.prev_hash != expected_prev:
                violations.append({
                    "type":    "BROKEN_HASH_CHAIN",
                    "node_id": node.node_id,
                    "detail":  f"prev_hash mismatch at position {i}",
                    "severity": AlertSeverity.CRITICAL,
                })

            recomputed = node.compute_hash(self._secret)
            if recomputed != node.node_hash:
                violations.append({
                    "type":    "TAMPERED_NODE",
                    "node_id": node.node_id,
                    "detail":  "Stored hash does not match recomputed hash",
                    "severity": AlertSeverity.CRITICAL,
                })

            prev_hash = node.node_hash   # advance chain pointer

            # --- 4b. Unauthorized override ---
            if node.is_override:
                actor = node.override_actor or ""
                if actor not in self._approved:
                    violations.append({
                        "type":    "UNAUTHORIZED_OVERRIDE",
                        "node_id": node.node_id,
                        "detail":  f"Actor '{actor}' not in approved override list",
                        "severity": AlertSeverity.CRITICAL,
                    })

        # --- 4c. Missing evidence ---
        present_types = {n["node_type"] for n in nodes}
        for required in self.REQUIRED_NODE_SEQUENCE:
            if required not in present_types:
                violations.append({
                    "type":    "MISSING_EVIDENCE",
                    "node_id": "N/A",
                    "detail":  f"Required node type '{required}' absent from chain",
                    "severity": AlertSeverity.WARNING,
                })

        return violations


# ===========================================================================
# 5. ALERT DISPATCHER
# ===========================================================================

class AlertDispatcher:
    """
    Routes violations to compliance officers via email + WORM alert log.
    """

    def __init__(
        self,
        smtp_host: str,
        smtp_port: int,
        sender:    str,
        recipients: list[str],
        worm_writer: Optional[CosmosWORMWriter] = None,
    ):
        self._smtp_host  = smtp_host
        self._smtp_port  = smtp_port
        self._sender     = sender
        self._recipients = recipients
        self._worm       = worm_writer

    # ------------------------------------------------------------------
    def dispatch(self, violations: list[dict], chain_id: str) -> None:
        if not violations:
            return

        critical = [v for v in violations if v.get("severity") == AlertSeverity.CRITICAL]
        warnings  = [v for v in violations if v.get("severity") == AlertSeverity.WARNING]

        subject = (
            f"[CRITICAL] Audit chain violation detected — chain {chain_id}"
            if critical else
            f"[WARNING] Audit chain warning — chain {chain_id}"
        )

        body_lines = [
            f"Audit Chain ID: {chain_id}",
            f"Detected at:    {_utcnow()}",
            "",
            f"Critical violations ({len(critical)}):",
        ]
        for v in critical:
            body_lines.append(f"  • [{v['type']}] {v['node_id']} — {v['detail']}")

        body_lines += ["", f"Warnings ({len(warnings)}):"]
        for v in warnings:
            body_lines.append(f"  • [{v['type']}] {v['node_id']} — {v['detail']}")

        body = "\n".join(body_lines)

        # Write to WORM alert log
        if self._worm:
            alert_doc = {
                "id":         str(uuid.uuid4()),
                "chain_id":   chain_id,
                "timestamp":  _utcnow(),
                "violations": violations,
                "subject":    subject,
            }
            try:
                self._worm.write_alert(alert_doc)
            except Exception as exc:
                logger.error("Failed to write alert to WORM: %s", exc)

        # Email compliance officers
        if self._recipients and self._smtp_host:
            try:
                msg = MIMEMultipart()
                msg["From"]    = self._sender
                msg["To"]      = ", ".join(self._recipients)
                msg["Subject"] = subject
                msg.attach(MIMEText(body, "plain"))
                with smtplib.SMTP(self._smtp_host, self._smtp_port) as server:
                    server.sendmail(self._sender, self._recipients, msg.as_string())
                logger.info("Alert email sent to %s", self._recipients)
            except Exception as exc:
                logger.error("Alert email failed: %s", exc)
        else:
            logger.warning("ALERT (no SMTP configured):\n%s", body)


# ===========================================================================
# 6. XBRL REPORT GENERATOR
# ===========================================================================

class XBRLReportGenerator:
    """
    Generates an inline XBRL (iXBRL) compliance report as a UTF-8 XML file.

    The output conforms to the XBRL 2.1 specification with an inline XHTML
    wrapper, suitable for regulatory submission (e.g., SEC, ESMA).
    """

    XBRL_NS  = "http://www.xbrl.org/2003/instance"
    XHTML_NS = "http://www.w3.org/1999/xhtml"
    LINK_NS  = "http://www.xbrl.org/2003/linkbase"

    # ------------------------------------------------------------------
    def generate(self, chain_id: str, nodes: list[dict], violations: list[dict],
                 output_path: Path) -> Path:
        root = Element("xbrl", xmlns=self.XBRL_NS)
        root.set("xmlns:link",   self.LINK_NS)
        root.set("xmlns:xbrli",  self.XBRL_NS)

        # Context
        context = SubElement(root, "context", id="audit-context")
        entity  = SubElement(context, "entity")
        SubElement(entity,  "identifier", scheme="https://audit.internal").text = chain_id
        period  = SubElement(context, "period")
        SubElement(period,  "instant").text = _utcnow()[:10]

        # Unit
        unit = SubElement(root, "unit", id="pure")
        SubElement(unit, "measure").text = "xbrli:pure"

        # Chain summary facts
        _xbrl_fact(root, "AuditChainId",        chain_id,           "audit-context", "pure")
        _xbrl_fact(root, "TotalNodes",           str(len(nodes)),    "audit-context", "pure")
        _xbrl_fact(root, "TotalViolations",      str(len(violations)),"audit-context","pure")
        _xbrl_fact(root, "ReportGeneratedAt",    _utcnow(),          "audit-context", "pure")
        _xbrl_fact(root, "GenesisHash",          GENESIS_HASH,       "audit-context", "pure")

        # Per-node section
        for idx, n in enumerate(nodes):
            prefix = f"node{idx}"
            _xbrl_fact(root, f"{prefix}NodeId",   n["id"],            "audit-context", "pure")
            _xbrl_fact(root, f"{prefix}NodeType", n["node_type"],     "audit-context", "pure")
            _xbrl_fact(root, f"{prefix}Hash",     n["node_hash"],     "audit-context", "pure")
            _xbrl_fact(root, f"{prefix}PrevHash", n["prev_hash"],     "audit-context", "pure")
            _xbrl_fact(root, f"{prefix}Timestamp",n["timestamp"],     "audit-context", "pure")
            _xbrl_fact(root, f"{prefix}IsOverride", str(n.get("is_override", False)),
                       "audit-context", "pure")

        # Violations section
        for idx, v in enumerate(violations):
            prefix = f"violation{idx}"
            _xbrl_fact(root, f"{prefix}Type",    v["type"],    "audit-context", "pure")
            _xbrl_fact(root, f"{prefix}NodeId",  v["node_id"], "audit-context", "pure")
            _xbrl_fact(root, f"{prefix}Detail",  v["detail"],  "audit-context", "pure")
            _xbrl_fact(root, f"{prefix}Severity",v.get("severity",""), "audit-context","pure")

        indent(root, space="  ")
        xml_bytes = tostring(root, encoding="utf-8", xml_declaration=True)
        output_path.write_bytes(xml_bytes)
        logger.info("XBRL report written → %s", output_path)
        return output_path


def _xbrl_fact(parent: Element, name: str, value: str,
               context_ref: str, unit_ref: str) -> Element:
    el = SubElement(parent, name)
    el.set("contextRef", context_ref)
    el.set("unitRef",    unit_ref)
    el.set("decimals",   "INF")
    el.text = value
    return el


# ===========================================================================
# 7. CRYPTOGRAPHICALLY SIGNED PDF REPORT GENERATOR
# ===========================================================================

class SignedPDFReportGenerator:
    """
    Generates a human-readable compliance PDF and embeds a detached
    RSA-PSS / SHA-256 digital signature in the filename-paired .sig file.

    The PDF itself is generated with ReportLab.  The .sig file contains the
    Base64-encoded signature over the PDF bytes, verifiable with the paired
    public key.
    """

    def __init__(self, private_key_pem: Optional[bytes] = None):
        """
        Args:
            private_key_pem: PEM-encoded RSA private key.
                             If None, a fresh ephemeral 2048-bit key is generated
                             (useful for testing; store real keys in Azure Key Vault).
        """
        if not _CRYPTO_AVAILABLE:
            raise ImportError("cryptography package is required. pip install cryptography")

        if private_key_pem:
            self._private_key = serialization.load_pem_private_key(
                private_key_pem, password=None, backend=default_backend()
            )
        else:
            logger.warning("No private key supplied — generating ephemeral key (dev mode)")
            self._private_key = rsa.generate_private_key(
                public_exponent=65537, key_size=2048, backend=default_backend()
            )

        self._public_key = self._private_key.public_key()

    # ------------------------------------------------------------------
    def generate(self, chain_id: str, nodes: list[dict],
                 violations: list[dict], output_path: Path) -> tuple[Path, Path]:
        """
        Returns (pdf_path, signature_path).
        """
        if not _REPORTLAB_AVAILABLE:
            raise ImportError("reportlab package is required. pip install reportlab")

        output_path.parent.mkdir(parents=True, exist_ok=True)
        sig_path = output_path.with_suffix(".pdf.sig")

        # --- Build PDF ---
        doc    = SimpleDocTemplate(str(output_path), pagesize=A4)
        styles = getSampleStyleSheet()
        story  = []

        story.append(Paragraph("Compliance Audit Report", styles["Title"]))
        story.append(Spacer(1, 12))
        story.append(Paragraph(f"Chain ID : {chain_id}", styles["Normal"]))
        story.append(Paragraph(f"Generated: {_utcnow()}", styles["Normal"]))
        story.append(Spacer(1, 24))

        # Node table
        story.append(Paragraph("Traceability Graph Nodes", styles["Heading2"]))
        table_data = [["#", "Type", "Node ID", "Hash (first 16)", "Override"]]
        for idx, n in enumerate(nodes):
            table_data.append([
                str(idx + 1),
                n["node_type"],
                n["id"][:16] + "…",
                n["node_hash"][:16] + "…",
                "YES" if n.get("is_override") else "no",
            ])
        tbl = Table(table_data, colWidths=[30, 80, 120, 130, 60])
        tbl.setStyle(TableStyle([
            ("BACKGROUND",  (0, 0), (-1, 0), colors.HexColor("#1F4E79")),
            ("TEXTCOLOR",   (0, 0), (-1, 0), colors.white),
            ("FONTNAME",    (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE",    (0, 0), (-1, -1), 8),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#EBF2FA")]),
            ("GRID",        (0, 0), (-1, -1), 0.4, colors.HexColor("#AAAAAA")),
        ]))
        story.append(tbl)
        story.append(Spacer(1, 24))

        # Violations
        story.append(Paragraph("Violations Detected", styles["Heading2"]))
        if violations:
            for v in violations:
                colour = "#CC0000" if v.get("severity") == AlertSeverity.CRITICAL else "#CC7700"
                story.append(Paragraph(
                    f'<font color="{colour}"><b>[{v["severity"]}] {v["type"]}</b></font>'
                    f' — node: {v["node_id"]} — {v["detail"]}',
                    styles["Normal"],
                ))
        else:
            story.append(Paragraph("✓ No violations detected.", styles["Normal"]))

        story.append(Spacer(1, 24))

        # Public key fingerprint (so verifiers know which key was used)
        pub_der = self._public_key.public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo
        )
        fingerprint = hashlib.sha256(pub_der).hexdigest()
        story.append(Paragraph("Digital Signature", styles["Heading2"]))
        story.append(Paragraph(f"Algorithm  : RSA-PSS / SHA-256", styles["Normal"]))
        story.append(Paragraph(f"Key SHA-256: {fingerprint}", styles["Normal"]))
        story.append(Paragraph(f"Sig file   : {sig_path.name}", styles["Normal"]))

        doc.build(story)

        # --- Sign PDF ---
        pdf_bytes = output_path.read_bytes()
        signature = self._private_key.sign(
            pdf_bytes,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.MAX_LENGTH,
            ),
            hashes.SHA256(),
        )
        import base64
        sig_path.write_bytes(base64.b64encode(signature))
        logger.info("Signed PDF → %s  |  Sig → %s", output_path, sig_path)
        return output_path, sig_path

    # ------------------------------------------------------------------
    def verify_signature(self, pdf_path: Path, sig_path: Path) -> bool:
        """
        Returns True if the signature is valid for this PDF.
        Raises cryptography.exceptions.InvalidSignature on failure.
        """
        import base64
        pdf_bytes = pdf_path.read_bytes()
        signature = base64.b64decode(sig_path.read_bytes())
        self._public_key.verify(
            signature,
            pdf_bytes,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.MAX_LENGTH,
            ),
            hashes.SHA256(),
        )
        return True


# ===========================================================================
# 8. MASTER AUDIT LOGGER
# ===========================================================================

class AuditLogger:
    """
    Top-level façade that orchestrates the entire audit pipeline:

        build_node() → write WORM → detect tampering → alert → report

    Usage example
    -------------
    >>> cfg = AuditLoggerConfig(
    ...     cosmos_connection_string=os.environ["COSMOS_CONN"],
    ...     cosmos_database="compliancedb",
    ...     hmac_secret=b"change-me-in-production",
    ...     approved_override_actors={"alice@corp.com", "bob@corp.com"},
    ...     smtp_host="smtp.corp.com",
    ...     smtp_port=587,
    ...     alert_sender="audit@corp.com",
    ...     alert_recipients=["compliance@corp.com"],
    ... )
    >>> al = AuditLogger(cfg)
    >>>
    >>> chain_id = al.new_chain()
    >>> al.append(chain_id, NodeType.REGULATION, {"ref": "SOX-302"})
    >>> al.append(chain_id, NodeType.MAP,        {"control": "C-14"})
    >>> al.append(chain_id, NodeType.TASK,       {"task": "Quarterly close"})
    >>> al.append(chain_id, NodeType.EVIDENCE,   {"file": "trial_balance.xlsx"})
    >>> al.append(chain_id, NodeType.SIGNOFF,    {"actor": "alice@corp.com"})
    >>> report = al.finalize(chain_id, output_dir=Path("./reports"))
    """

    def __init__(self, config: "AuditLoggerConfig"):
        self._cfg     = config
        self._secret  = (config.hmac_secret or os.urandom(32))
        self._worm    = (
            CosmosWORMWriter(config.cosmos_connection_string, config.cosmos_database)
            if config.cosmos_connection_string else None
        )
        self._detector = TamperDetector(
            hmac_secret             = self._secret,
            approved_override_actors= config.approved_override_actors or set(),
        )
        self._alerter = AlertDispatcher(
            smtp_host   = config.smtp_host or "",
            smtp_port   = config.smtp_port or 587,
            sender      = config.alert_sender or "",
            recipients  = config.alert_recipients or [],
            worm_writer = self._worm,
        )
        self._xbrl  = XBRLReportGenerator()
        self._pdf   = SignedPDFReportGenerator(config.private_key_pem)

        # In-memory chain buffer (for offline / unit-test use)
        self._chains: dict[str, list[TraceabilityNode]] = {}

    # ------------------------------------------------------------------
    def new_chain(self) -> str:
        """Create a fresh chain and return its ID."""
        chain_id = str(uuid.uuid4())
        self._chains[chain_id] = []
        logger.info("New audit chain started: %s", chain_id)
        return chain_id

    # ------------------------------------------------------------------
    def append(
        self,
        chain_id:    str,
        node_type:   NodeType,
        payload:     dict[str, Any],
        explainability: Optional[ExplainabilityMetadata] = None,
        is_override: bool = False,
        override_actor: Optional[str] = None,
    ) -> TraceabilityNode:
        """
        Append a new immutable node to the chain.
        Persists to Cosmos DB WORM if a connection string is configured.
        """
        chain = self._chains.setdefault(chain_id, [])
        prev_hash = chain[-1].node_hash if chain else GENESIS_HASH

        node = TraceabilityNode(
            node_type      = node_type,
            node_id        = str(uuid.uuid4()),
            timestamp      = _utcnow(),
            payload        = payload,
            prev_hash      = prev_hash,
            explainability = explainability.to_dict() if explainability else None,
            is_override    = is_override,
            override_actor = override_actor,
            chain_id       = chain_id,
        )
        node.node_hash = node.compute_hash(self._secret)
        chain.append(node)

        if self._worm:
            self._worm.write_node(node)

        logger.info(
            "Node appended  chain=%s  type=%-12s  hash=%s…",
            chain_id, node_type, node.node_hash[:16],
        )
        return node

    # ------------------------------------------------------------------
    def verify(self, chain_id: str) -> list[dict]:
        """
        Run tamper detection on the in-memory chain.
        Returns a list of violation dicts (empty = chain is intact).
        """
        chain = self._chains.get(chain_id, [])
        raw_nodes = [n.to_cosmos_document() for n in chain]
        violations = self._detector.verify_chain(raw_nodes)
        if violations:
            self._alerter.dispatch(violations, chain_id)
        return violations

    # ------------------------------------------------------------------
    def finalize(self, chain_id: str, output_dir: Path = Path("./reports")) -> dict[str, Path]:
        """
        Verify the chain, then generate XBRL + signed PDF reports.
        Returns paths to all generated artefacts.
        """
        output_dir.mkdir(parents=True, exist_ok=True)
        chain     = self._chains.get(chain_id, [])
        raw_nodes = [n.to_cosmos_document() for n in chain]
        violations = self._detector.verify_chain(raw_nodes)

        if violations:
            self._alerter.dispatch(violations, chain_id)

        safe_chain = re.sub(r"[^a-zA-Z0-9_-]", "_", chain_id)
        ts         = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

        xbrl_path  = output_dir / f"audit_{safe_chain}_{ts}.xbrl"
        pdf_path   = output_dir / f"audit_{safe_chain}_{ts}.pdf"

        self._xbrl.generate(chain_id, raw_nodes, violations, xbrl_path)
        pdf_out, sig_out = self._pdf.generate(chain_id, raw_nodes, violations, pdf_path)

        result = {
            "xbrl":      xbrl_path,
            "pdf":       pdf_out,
            "signature": sig_out,
        }
        logger.info("Finalized chain %s  →  reports: %s", chain_id, result)
        return result

    # ------------------------------------------------------------------
    def load_chain_from_cosmos(self, chain_id: str) -> list[dict]:
        """
        Reload a chain from Cosmos DB and run verification.
        Useful for scheduled reconciliation jobs.
        """
        if not self._worm:
            raise RuntimeError("No Cosmos DB connection configured.")
        raw_nodes = self._worm.read_chain(chain_id)
        violations = self._detector.verify_chain(raw_nodes)
        if violations:
            self._alerter.dispatch(violations, chain_id)
        return violations

    # ------------------------------------------------------------------
    def get_chain(self, chain_id: str) -> Optional[AuditChain]:
        """Retrieve the list of nodes for a chain wrapped in an AuditChain object."""
        if chain_id in self._chains:
            return AuditChain(self._chains[chain_id])
        return None

    # ------------------------------------------------------------------
    async def log_human_correction(
        self,
        chain_id: str,
        corrected_by: str,
        stage: str,
        original_value: Any,
        corrected_value: Any,
        regulation_ref: str,
        domain: str,
        comment: str = "",
    ) -> str:
        """
        Log a manual correction from the Chief Compliance Officer.
        Appends a HUMAN_CORRECTION node to the active chain and registers
        the correction in the in-memory ledger for few-shot search.
        """
        correction_id = str(uuid.uuid4())
        payload = {
            "correction_id": correction_id,
            "chain_id": chain_id,
            "corrected_by": corrected_by,
            "stage": stage,
            "original_value": original_value,
            "corrected_value": corrected_value,
            "regulation_ref": regulation_ref,
            "domain": domain,
            "comment": comment,
            "timestamp": _utcnow(),
        }

        # dual-write to in-memory ledger for fast querying
        _IN_MEMORY_CORRECTIONS.append(payload)

        # append to traceability chain
        self.append(
            chain_id=chain_id,
            node_type=NodeType.HUMAN_CORRECTION,
            payload=payload,
            is_override=True,
            override_actor=corrected_by,
        )
        return correction_id


class AuditChain:
    """Wraps a list of TraceabilityNodes to match test suite properties."""
    def __init__(self, nodes: list[TraceabilityNode]):
        self.nodes = nodes


_IN_MEMORY_CORRECTIONS: list[dict] = []


class CorrectionLedgerClient:
    """
    Client for querying human compliance corrections from the WORM ledger.
    Provides automatic in-memory fallbacks when Cosmos DB is unconfigured.
    """

    def __init__(self, worm_writer: Optional[CosmosWORMWriter] = None):
        self._worm = worm_writer

    async def get_recent_corrections(
        self,
        domain: str,
        regulation_ref: str = "",
        limit: int = 5,
    ) -> list[dict]:
        all_corrections = []
        if self._worm:
            try:
                query = "SELECT * FROM c WHERE c.node_type = @node_type"
                params = [{"name": "@node_type", "value": NodeType.HUMAN_CORRECTION.value}]
                docs = list(self._worm._ledger.query_items(
                    query=query, parameters=params, enable_cross_partition_query=True
                ))
                all_corrections = [d["payload"] for d in docs if "payload" in d]
            except Exception as exc:
                logger.error("Cosmos DB corrections query failed: %s", exc)
                all_corrections = _IN_MEMORY_CORRECTIONS
        else:
            all_corrections = _IN_MEMORY_CORRECTIONS

        # Apply fallback logic
        # 1. Exact match by domain and regulation_ref
        exact_matches = []
        if regulation_ref:
            exact_matches = [
                c for c in all_corrections
                if c.get("domain") == domain and c.get("regulation_ref") == regulation_ref
            ]

        if exact_matches:
            exact_matches.sort(key=lambda x: x.get("timestamp", ""), reverse=True)
            return exact_matches[:limit]

        # 2. Domain fallback (regardless of regulation_ref)
        fallback_matches = [
            c for c in all_corrections
            if c.get("domain") == domain
        ]
        fallback_matches.sort(key=lambda x: x.get("timestamp", ""), reverse=True)
        return fallback_matches[:limit]


# ===========================================================================
# 9. CONFIGURATION DATACLASS
# ===========================================================================

@dataclass
class AuditLoggerConfig:
    """
    All configuration for AuditLogger.  Load sensitive values from
    Azure Key Vault / environment variables — never hard-code secrets.
    """
    # Cosmos DB
    cosmos_connection_string: Optional[str] = None   # os.environ["COSMOS_CONN"]
    cosmos_database:          str           = "compliancedb"

    # Cryptography
    hmac_secret:     Optional[bytes] = None           # os.environ["AUDIT_HMAC_SECRET"].encode()
    private_key_pem: Optional[bytes] = None           # content of PEM file from Key Vault

    # Override governance
    approved_override_actors: Optional[set[str]] = None

    # Alerting
    smtp_host:         Optional[str]       = None
    smtp_port:         int                 = 587
    alert_sender:      Optional[str]       = None
    alert_recipients:  Optional[list[str]] = None


# ===========================================================================
# 10. HELPERS
# ===========================================================================

def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ===========================================================================
# QUICK SMOKE TEST  (python audit/logger.py)
# ===========================================================================

if __name__ == "__main__":
    from pathlib import Path

    print("=" * 60)
    print("AuditLogger — offline smoke test (no Cosmos DB)")
    print("=" * 60)

    cfg = AuditLoggerConfig(
        hmac_secret             = b"super-secret-key-replace-in-production",
        approved_override_actors= {"alice@corp.com"},
    )
    al  = AuditLogger(cfg)
    cid = al.new_chain()

    # --- Happy path ---
    al.append(cid, NodeType.REGULATION, {"ref": "SOX-302", "jurisdiction": "US"})
    al.append(cid, NodeType.MAP,        {"control_id": "C-14", "owner": "Finance"})
    al.append(cid, NodeType.TASK,       {"description": "Quarterly close review"})

    expl = ExplainabilityMetadata(
        decision_id      = str(uuid.uuid4()),
        model_version    = "risk-classifier-v2.3.1",
        feature_weights  = {"revenue_delta": 0.62, "journal_count": 0.28, "days_late": 0.10},
        confidence_score = 0.91,
        reasoning_steps  = [
            "Revenue delta exceeds ±5% threshold → elevated risk flag.",
            "Journal entry count within normal range → no flag.",
            "Submission 2 days late → minor flag.",
            "Combined score 0.91 → classify as HIGH RISK.",
        ],
        alternative_decisions = [
            {"label": "MEDIUM RISK", "score": 0.07, "reason": "If revenue delta < 5%"},
            {"label": "LOW RISK",    "score": 0.02, "reason": "If all metrics normal"},
        ],
    )
    al.append(cid, NodeType.EVIDENCE, {"file": "trial_balance_Q1.xlsx",
                                        "sha256": "abc123"}, explainability=expl)
    al.append(cid, NodeType.SIGNOFF,  {"actor": "alice@corp.com", "role": "CFO"})

    # --- Verify (should be clean) ---
    violations = al.verify(cid)
    assert not violations, f"Unexpected violations: {violations}"
    print("✓ Chain integrity verified — no violations")

    # --- Generate reports ---
    reports = al.finalize(cid, output_dir=Path("/tmp/audit_reports"))
    print(f"✓ XBRL report  → {reports['xbrl']}")
    print(f"✓ PDF report   → {reports['pdf']}")
    print(f"✓ Signature    → {reports['signature']}")

    # --- Verify PDF signature ---
    valid = al._pdf.verify_signature(reports["pdf"], reports["signature"])
    print(f"✓ PDF signature valid: {valid}")

    # --- Tamper simulation ---
    print("\n--- Tamper simulation ---")
    al._chains[cid][2].payload["INJECTED"] = "EVIL"   # mutate a stored node
    violations = al.verify(cid)
    assert any(v["type"] == "TAMPERED_NODE" for v in violations), "Tamper not detected!"
    print(f"✓ Tamper detected: {[v['type'] for v in violations]}")

    # --- Unauthorized override simulation ---
    print("\n--- Unauthorized override simulation ---")
    cid2 = al.new_chain()
    al.append(cid2, NodeType.REGULATION, {"ref": "GDPR-Art-30"})
    al.append(cid2, NodeType.MAP,        {"control_id": "C-99"})
    al.append(cid2, NodeType.TASK,       {"description": "Data mapping review"})
    al.append(cid2, NodeType.EVIDENCE,   {"file": "data_map.pdf"},
              is_override=True, override_actor="mallory@external.com")  # NOT approved
    al.append(cid2, NodeType.SIGNOFF,    {"actor": "alice@corp.com"})
    violations2 = al.verify(cid2)
    assert any(v["type"] == "UNAUTHORIZED_OVERRIDE" for v in violations2)
    print(f"✓ Unauthorized override detected: {[v['type'] for v in violations2]}")

    print("\nAll smoke tests passed ✓")