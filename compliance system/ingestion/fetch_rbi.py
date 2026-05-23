"""
ingestion/fetch_rbi.py
----------------------
RBI Notifications Ingestion Engine — Azure (India Central)

Pipeline:
  1. Fetch RBI RSS/Atom feed → deduplicate via SHA-256 registry
  2. Download PDFs → upload to Azure Blob Storage
       /regulatory-store/rbi/{year}/{month}/{document_id}.pdf
  3. Parse PDFs with Azure Document Intelligence (Tesseract OCR fallback)
  4. Normalize to standard JSON schema
  5. DLQ (Azure Service Bus) for failed documents

Environment variables (set via Azure Key Vault / App Configuration):
  AZURE_STORAGE_CONN_STR          – Blob Storage connection string
  AZURE_DOC_INTEL_ENDPOINT        – Document Intelligence endpoint (India Central)
  AZURE_DOC_INTEL_KEY             – Document Intelligence API key
  AZURE_SERVICEBUS_CONN_STR       – Service Bus namespace connection string
  AZURE_SERVICEBUS_DLQ_QUEUE      – DLQ queue name (e.g. "rbi-ingestion-dlq")
  AZURE_HASH_TABLE_CONN_STR       – Azure Table Storage connection string
  AZURE_HASH_TABLE_NAME           – Table name for SHA-256 deduplication registry
  RBI_FEED_URL                    – RBI notifications RSS/Atom feed URL
                                    (default: https://www.rbi.org.in/Scripts/RSS.aspx)
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import re
import tempfile
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from urllib.parse import urlparse

import feedparser
import requests
from azure.core.exceptions import AzureError, ResourceExistsError
from azure.data.tables import TableClient, TableServiceClient
from azure.servicebus import ServiceBusClient, ServiceBusMessage
from azure.storage.blob import BlobServiceClient, ContentSettings

# Azure Document Intelligence (Form Recognizer v4 SDK)
from azure.ai.documentintelligence import DocumentIntelligenceClient
from azure.ai.documentintelligence.models import AnalyzeDocumentRequest
from azure.core.credentials import AzureKeyCredential

logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

REGULATOR = "rbi"
BLOB_CONTAINER = "regulatory-store"
AZURE_REGION = "indiacentral"

DEFAULT_RBI_FEED_URL = "https://www.rbi.org.in/Scripts/RSS.aspx"

# HTTP timeouts (seconds)
FEED_TIMEOUT = 30
PDF_DOWNLOAD_TIMEOUT = 60
PDF_CHUNK_SIZE = 1024 * 256  # 256 KB

# Document Intelligence model — prebuilt layout captures text + tables + headings
DOC_INTEL_MODEL = "prebuilt-layout"

# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass
class RawFeedEntry:
    """A single entry parsed from the RBI RSS/Atom feed."""

    entry_id: str          # feed-provided GUID or link
    title: str
    link: str              # URL to the notification / PDF
    published: datetime
    summary: str = ""
    sha256: str = field(init=False)

    def __post_init__(self) -> None:
        # Deterministic hash: SHA-256 over (link + title) — stable across re-fetches
        payload = f"{self.link}|{self.title}".encode("utf-8")
        self.sha256 = hashlib.sha256(payload).hexdigest()


@dataclass
class ExtractedContent:
    raw_text: str
    tables: list[dict[str, Any]]
    section_headings: list[str]
    page_count: int
    extraction_method: str   # "document_intelligence" | "tesseract"


@dataclass
class NormalizedDocument:
    document_id: str
    regulator: str
    source_url: str
    blob_path: str
    published_date: str      # ISO-8601
    title: str
    sha256: str
    extracted_content: dict[str, Any]
    ingested_at: str         # ISO-8601


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _env(key: str, default: str | None = None) -> str:
    value = os.environ.get(key, default)
    if value is None:
        raise EnvironmentError(
            f"Required environment variable '{key}' is not set. "
            "Ensure it is injected from Azure Key Vault or App Configuration."
        )
    return value


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_filename(url: str) -> str:
    """Derive a clean filename from a URL."""
    parsed = urlparse(url)
    name = os.path.basename(parsed.path) or "document"
    # Strip non-alphanumeric except hyphens/underscores/dots
    name = re.sub(r"[^\w.\-]", "_", name)
    return name if name.endswith(".pdf") else f"{name}.pdf"


# ---------------------------------------------------------------------------
# SHA-256 Deduplication Registry (Azure Table Storage)
# ---------------------------------------------------------------------------


class HashRegistry:
    """
    Tracks ingested documents by their SHA-256 fingerprint.
    Uses Azure Table Storage for a cheap, serverless registry in India Central.

    Partition key : REGULATOR constant ("rbi")
    Row key       : sha256 hex digest
    """

    def __init__(self) -> None:
        conn_str = _env("AZURE_HASH_TABLE_CONN_STR")
        table_name = _env("AZURE_HASH_TABLE_NAME", "rbiHashRegistry")

        service_client = TableServiceClient.from_connection_string(conn_str)
        try:
            service_client.create_table(table_name)
            logger.info("Created hash registry table '%s'.", table_name)
        except ResourceExistsError:
            pass  # Table already exists — expected in steady state

        self._client: TableClient = service_client.get_table_client(table_name)

    def exists(self, sha256: str) -> bool:
        try:
            self._client.get_entity(partition_key=REGULATOR, row_key=sha256)
            return True
        except Exception:
            return False

    def register(self, sha256: str, metadata: dict[str, str]) -> None:
        entity = {
            "PartitionKey": REGULATOR,
            "RowKey": sha256,
            "registered_at": _utcnow_iso(),
            **{k: str(v) for k, v in metadata.items()},
        }
        self._client.upsert_entity(entity)


# ---------------------------------------------------------------------------
# Blob Storage
# ---------------------------------------------------------------------------


class BlobStore:
    """
    Manages raw PDF uploads to Azure Blob Storage.
    Container : regulatory-store
    Path      : /regulatory-store/rbi/{year}/{month}/{document_id}.pdf
    """

    def __init__(self) -> None:
        conn_str = _env("AZURE_STORAGE_CONN_STR")
        self._service = BlobServiceClient.from_connection_string(conn_str)
        self._container = self._service.get_container_client(BLOB_CONTAINER)
        try:
            self._container.create_container()
            logger.info("Created blob container '%s'.", BLOB_CONTAINER)
        except ResourceExistsError:
            pass

    def upload_pdf(
        self,
        pdf_bytes: bytes,
        document_id: str,
        published_date: datetime,
    ) -> str:
        """Upload PDF and return its blob path."""
        year = published_date.strftime("%Y")
        month = published_date.strftime("%m")
        blob_path = f"{REGULATOR}/{year}/{month}/{document_id}.pdf"

        blob_client = self._container.get_blob_client(blob_path)
        blob_client.upload_blob(
            data=pdf_bytes,
            overwrite=True,
            content_settings=ContentSettings(content_type="application/pdf"),
            metadata={
                "document_id": document_id,
                "regulator": REGULATOR,
                "region": AZURE_REGION,
            },
        )
        logger.info("Uploaded PDF to blob path: %s", blob_path)
        return blob_path

    def get_pdf_bytes(self, blob_path: str) -> bytes:
        blob_client = self._container.get_blob_client(blob_path)
        downloader = blob_client.download_blob()
        return downloader.readall()


# ---------------------------------------------------------------------------
# PDF Parsing — Azure Document Intelligence + Tesseract fallback
# ---------------------------------------------------------------------------


class DocumentParser:
    """
    Primary  : Azure Document Intelligence (prebuilt-layout model)
    Fallback : Tesseract OCR via pytesseract + pdf2image
    """

    def __init__(self) -> None:
        endpoint = _env("AZURE_DOC_INTEL_ENDPOINT")
        key = _env("AZURE_DOC_INTEL_KEY")
        self._doc_intel = DocumentIntelligenceClient(
            endpoint=endpoint,
            credential=AzureKeyCredential(key),
        )

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def parse(self, pdf_bytes: bytes) -> ExtractedContent:
        try:
            return self._parse_with_doc_intel(pdf_bytes)
        except Exception as exc:
            logger.warning(
                "Document Intelligence failed (%s). Falling back to Tesseract OCR.", exc
            )
            return self._parse_with_tesseract(pdf_bytes)

    # ------------------------------------------------------------------
    # Azure Document Intelligence
    # ------------------------------------------------------------------

    def _parse_with_doc_intel(self, pdf_bytes: bytes) -> ExtractedContent:
        poller = self._doc_intel.begin_analyze_document(
            model_id=DOC_INTEL_MODEL,
            analyze_request=AnalyzeDocumentRequest(bytes_source=pdf_bytes),
        )
        result = poller.result()

        raw_text_parts: list[str] = []
        tables: list[dict[str, Any]] = []
        section_headings: list[str] = []
        page_count = len(result.pages) if result.pages else 0

        # ---- Text & headings ----
        for page in result.pages or []:
            for line in page.lines or []:
                raw_text_parts.append(line.content)

        # Paragraphs carry role metadata (e.g., "title", "sectionHeading")
        for para in result.paragraphs or []:
            role = (para.role or "").lower()
            if role in ("title", "sectionheading", "heading"):
                section_headings.append(para.content)

        # ---- Tables ----
        for tbl_idx, table in enumerate(result.tables or []):
            rows_dict: dict[int, dict[int, str]] = {}
            for cell in table.cells or []:
                rows_dict.setdefault(cell.row_index, {})[cell.column_index] = (
                    cell.content
                )
            serialized_rows = [
                [rows_dict[r].get(c, "") for c in range(table.column_count)]
                for r in range(table.row_count)
            ]
            tables.append(
                {
                    "table_index": tbl_idx,
                    "row_count": table.row_count,
                    "column_count": table.column_count,
                    "rows": serialized_rows,
                }
            )

        return ExtractedContent(
            raw_text="\n".join(raw_text_parts),
            tables=tables,
            section_headings=section_headings,
            page_count=page_count,
            extraction_method="document_intelligence",
        )

    # ------------------------------------------------------------------
    # Tesseract OCR fallback
    # ------------------------------------------------------------------

    def _parse_with_tesseract(self, pdf_bytes: bytes) -> ExtractedContent:
        # Late imports so the module doesn't hard-fail if Tesseract isn't
        # installed in environments that always use Document Intelligence.
        try:
            import pytesseract
            from pdf2image import convert_from_bytes
        except ImportError as e:
            raise RuntimeError(
                "Tesseract fallback requires 'pytesseract' and 'pdf2image'. "
                f"Install them or ensure Document Intelligence is reachable. ({e})"
            ) from e

        images = convert_from_bytes(pdf_bytes, dpi=300)
        text_parts: list[str] = []

        for img in images:
            page_text = pytesseract.image_to_string(img, lang="eng")
            text_parts.append(page_text)

        full_text = "\n".join(text_parts)

        # Heuristic heading detection: ALL-CAPS lines or lines ending with ":"
        headings = [
            line.strip()
            for line in full_text.splitlines()
            if line.strip() and (
                line.strip().isupper() or re.match(r"^[A-Z][^.?!]{5,80}:$", line.strip())
            )
        ]

        return ExtractedContent(
            raw_text=full_text,
            tables=[],           # Tesseract doesn't extract structured tables
            section_headings=headings,
            page_count=len(images),
            extraction_method="tesseract",
        )


# ---------------------------------------------------------------------------
# Dead Letter Queue (Azure Service Bus)
# ---------------------------------------------------------------------------


class DeadLetterQueue:
    """
    Publishes failed-document payloads to an Azure Service Bus queue for
    manual review or retry by downstream consumers.
    """

    def __init__(self) -> None:
        conn_str = _env("AZURE_SERVICEBUS_CONN_STR")
        self._queue_name = _env("AZURE_SERVICEBUS_DLQ_QUEUE", "rbi-ingestion-dlq")
        self._client = ServiceBusClient.from_connection_string(conn_str)

    def send(self, document_id: str, source_url: str, error: str) -> None:
        payload = {
            "document_id": document_id,
            "regulator": REGULATOR,
            "source_url": source_url,
            "error": error,
            "failed_at": _utcnow_iso(),
            "region": AZURE_REGION,
        }
        message_body = json.dumps(payload)

        with self._client:
            sender = self._client.get_queue_sender(queue_name=self._queue_name)
            with sender:
                msg = ServiceBusMessage(
                    body=message_body,
                    subject="rbi-ingestion-failure",
                    application_properties={
                        "document_id": document_id,
                        "regulator": REGULATOR,
                    },
                )
                sender.send_messages(msg)
                logger.warning(
                    "Sent document '%s' to DLQ. Reason: %s", document_id, error
                )


# ---------------------------------------------------------------------------
# Feed Fetcher
# ---------------------------------------------------------------------------


class FeedFetcher:
    """Fetches and parses the RBI RSS/Atom notifications feed."""

    def __init__(self, feed_url: str) -> None:
        self._feed_url = feed_url

    def fetch_entries(self) -> list[RawFeedEntry]:
        logger.info("Fetching RBI feed: %s", self._feed_url)
        resp = requests.get(self._feed_url, timeout=FEED_TIMEOUT)
        resp.raise_for_status()

        feed = feedparser.parse(resp.content)
        entries: list[RawFeedEntry] = []

        for item in feed.entries:
            link = getattr(item, "link", "") or ""
            title = getattr(item, "title", "Untitled") or "Untitled"
            entry_id = getattr(item, "id", link) or link
            summary = getattr(item, "summary", "") or ""

            # Parse published date; fall back to now
            pub_parsed = getattr(item, "published_parsed", None)
            if pub_parsed:
                published = datetime(*pub_parsed[:6], tzinfo=timezone.utc)
            else:
                published = datetime.now(timezone.utc)

            if not link:
                logger.debug("Skipping entry without link: %s", title)
                continue

            entries.append(
                RawFeedEntry(
                    entry_id=entry_id,
                    title=title,
                    link=link,
                    published=published,
                    summary=summary,
                )
            )

        logger.info("Parsed %d entries from feed.", len(entries))
        return entries

    @staticmethod
    def download_pdf(url: str) -> bytes:
        """Download a PDF from a URL, following redirects."""
        logger.debug("Downloading PDF: %s", url)
        with requests.get(url, timeout=PDF_DOWNLOAD_TIMEOUT, stream=True) as resp:
            resp.raise_for_status()
            buf = io.BytesIO()
            for chunk in resp.iter_content(chunk_size=PDF_CHUNK_SIZE):
                buf.write(chunk)
        return buf.getvalue()


# ---------------------------------------------------------------------------
# Schema Normalizer
# ---------------------------------------------------------------------------


def normalize(
    entry: RawFeedEntry,
    blob_path: str,
    content: ExtractedContent,
) -> NormalizedDocument:
    """
    Produces the canonical JSON-serializable document schema.

    Schema
    ------
    {
        "document_id"       : str,          # UUID v4
        "regulator"         : "rbi",
        "source_url"        : str,
        "blob_path"         : str,          # Azure Blob path
        "published_date"    : str,          # ISO-8601
        "title"             : str,
        "sha256"            : str,          # SHA-256 of (link|title)
        "extracted_content" : {
            "raw_text"          : str,
            "tables"            : [...],
            "section_headings"  : [...],
            "page_count"        : int,
            "extraction_method" : str
        },
        "ingested_at"       : str           # ISO-8601
    }
    """
    return NormalizedDocument(
        document_id=str(uuid.uuid4()),
        regulator=REGULATOR,
        source_url=entry.link,
        blob_path=blob_path,
        published_date=entry.published.isoformat(),
        title=entry.title,
        sha256=entry.sha256,
        extracted_content={
            "raw_text": content.raw_text,
            "tables": content.tables,
            "section_headings": content.section_headings,
            "page_count": content.page_count,
            "extraction_method": content.extraction_method,
        },
        ingested_at=_utcnow_iso(),
    )


def document_to_dict(doc: NormalizedDocument) -> dict[str, Any]:
    return {
        "document_id": doc.document_id,
        "regulator": doc.regulator,
        "source_url": doc.source_url,
        "blob_path": doc.blob_path,
        "published_date": doc.published_date,
        "title": doc.title,
        "sha256": doc.sha256,
        "extracted_content": doc.extracted_content,
        "ingested_at": doc.ingested_at,
    }


# ---------------------------------------------------------------------------
# Ingestion Engine
# ---------------------------------------------------------------------------


class RBIIngestionEngine:
    """
    Orchestrates the full RBI notification ingestion pipeline.

    Usage
    -----
    engine = RBIIngestionEngine()
    results = engine.run()
    """

    def __init__(self, feed_url: str | None = None) -> None:
        self._feed_url = feed_url or _env("RBI_FEED_URL", DEFAULT_RBI_FEED_URL)
        self._fetcher = FeedFetcher(self._feed_url)
        self._registry = HashRegistry()
        self._blob_store = BlobStore()
        self._parser = DocumentParser()
        self._dlq = DeadLetterQueue()

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    def run(self) -> list[dict[str, Any]]:
        """
        Execute one full ingestion cycle.

        Returns a list of normalized document dicts for all successfully
        ingested documents in this run.
        """
        logger.info("=== RBI Ingestion Engine starting (region: %s) ===", AZURE_REGION)
        entries = self._fetcher.fetch_entries()

        ingested: list[dict[str, Any]] = []
        skipped = 0
        failed = 0

        for entry in entries:
            if self._registry.exists(entry.sha256):
                logger.debug("Skipping duplicate: %s (sha256=%s)", entry.title, entry.sha256[:12])
                skipped += 1
                continue

            result = self._process_entry(entry)
            if result is not None:
                ingested.append(result)
            else:
                failed += 1

        logger.info(
            "=== Ingestion cycle complete — ingested=%d | skipped=%d | failed=%d ===",
            len(ingested),
            skipped,
            failed,
        )
        return ingested


# ---------------------------------------------------------------------------
# Compatibility shims for the orchestrator (main.py)
# ---------------------------------------------------------------------------


class RegulatorySource(Enum):
    RBI = "RBI"


@dataclass
class FetchConfig:
    adls_connection_string: str | None = None
    adls_account_name: str | None = None
    adls_container_name: str = BLOB_CONTAINER
    cosmos_connection_string: str | None = None
    form_recognizer_endpoint: str | None = None
    form_recognizer_key: str | None = None
    azure_region: str = AZURE_REGION
    source_urls: dict[str, str] = field(default_factory=lambda: {"RBI": DEFAULT_RBI_FEED_URL})


@dataclass
class IngestionResult:
    doc_id: str
    source: RegulatorySource
    blob_path: str
    metadata: dict
    extracted_text: str
    content_hash: str
    ocr_used: bool
    confidence: float


class IngestionEngine:
    """Lightweight compatibility wrapper around RBIIngestionEngine.

    Provides the minimal interface expected by `main.py`:
        engine = IngestionEngine(fetch_cfg)
        engine.ingest(url, RegulatorySource.RBI)
    """

    def __init__(self, fetch_cfg: FetchConfig | None = None) -> None:
        self._cfg = fetch_cfg or FetchConfig()

    def ingest(self, url: str, source: RegulatorySource) -> IngestionResult:
        # Use RBIIngestionEngine to perform a single ingestion run for the
        # provided feed URL and return the first ingested document as a
        # compatibility IngestionResult.
        engine = RBIIngestionEngine(feed_url=url)
        results = engine.run()
        if not results:
            # No documents found — return an empty placeholder
            return IngestionResult(
                doc_id="", source=source, blob_path=url, metadata={},
                extracted_text="", content_hash="", ocr_used=False, confidence=0.0
            )

        doc = results[0]
        extracted = doc.get("extracted_content", {})
        method = extracted.get("extraction_method", "document_intelligence")
        return IngestionResult(
            doc_id=doc.get("document_id", ""),
            source=source,
            blob_path=doc.get("blob_path", url),
            metadata={
                "mime_type": "application/pdf",
            },
            extracted_text=extracted.get("raw_text", ""),
            content_hash=doc.get("sha256", ""),
            ocr_used=(method == "tesseract"),
            confidence=0.95 if extracted.get("raw_text") else 0.0,
        )

    # ------------------------------------------------------------------
    # Per-entry processing
    # ------------------------------------------------------------------

    def _process_entry(self, entry: RawFeedEntry) -> dict[str, Any] | None:
        document_id = str(uuid.uuid4())
        logger.info("Processing [%s] %s", document_id[:8], entry.title)

        # Step 1 — Download PDF
        try:
            pdf_bytes = self._fetcher.download_pdf(entry.link)
        except Exception as exc:
            error_msg = f"PDF download failed: {exc}"
            logger.error(error_msg)
            self._dlq.send(document_id, entry.link, error_msg)
            return None

        # Step 2 — Upload raw PDF to Blob Storage
        try:
            blob_path = self._blob_store.upload_pdf(pdf_bytes, document_id, entry.published)
        except AzureError as exc:
            error_msg = f"Blob upload failed: {exc}"
            logger.error(error_msg)
            self._dlq.send(document_id, entry.link, error_msg)
            return None

        # Step 3 — Parse PDF
        try:
            content = self._parser.parse(pdf_bytes)
        except Exception as exc:
            error_msg = f"PDF parsing failed: {exc}"
            logger.error(error_msg)
            self._dlq.send(document_id, entry.link, error_msg)
            return None

        # Step 4 — Normalize
        doc = normalize(entry, blob_path, content)

        # Step 5 — Register hash (only on full success)
        self._registry.register(
            entry.sha256,
            {
                "document_id": doc.document_id,
                "title": entry.title[:200],
                "source_url": entry.link[:500],
            },
        )

        logger.info(
            "Successfully ingested document_id=%s method=%s pages=%d",
            doc.document_id,
            content.extraction_method,
            content.page_count,
        )
        return document_to_dict(doc)


# ---------------------------------------------------------------------------
# CLI entrypoint (for local testing / Azure Container Jobs)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    feed_url = sys.argv[1] if len(sys.argv) > 1 else None
    engine = RBIIngestionEngine(feed_url=feed_url)
    results = engine.run()

    output_path = os.path.join(tempfile.gettempdir(), "rbi_ingestion_results.json")
    with open(output_path, "w", encoding="utf-8") as fh:
        json.dump(results, fh, indent=2, ensure_ascii=False)

    print(f"Ingested {len(results)} documents. Output written to: {output_path}")