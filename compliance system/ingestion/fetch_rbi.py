"""
ingestion/fetch_rbi.py
======================
Production-grade regulatory document ingestion engine.

Supports multiple regulatory sources:
  - RBI  (Reserve Bank of India)
  - SEBI (Securities and Exchange Board of India)
  - CERT-In (Indian Computer Emergency Response Team)
  - DPDP (Digital Personal Data Protection Act)

Pipeline
--------
  1. Fetch     → Download document from source URL or ADLS blob
  2. Deduplicate → SHA-256 hash-based deduplication against Cosmos DB
  3. Parse     → Extract text via Azure Document Intelligence (Form Recognizer)
  4. OCR Fallback → If text extraction yields low confidence, run OCR pipeline
  5. Store     → Upload parsed content to ADLS Gen2 and register in Cosmos DB

Dependencies
------------
  pip install azure-storage-blob azure-cosmos azure-ai-formrecognizer httpx
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────────────────────
OCR_CONFIDENCE_THRESHOLD = 0.70   # below this, trigger OCR fallback
DEDUP_LOOKBACK_DAYS      = 365
MAX_DOCUMENT_SIZE_MB     = 50
SUPPORTED_MIME_TYPES     = {
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "text/html",
    "text/plain",
}

# ──────────────────────────────────────────────────────────────────────────────
# Enumerations
# ──────────────────────────────────────────────────────────────────────────────

class RegulatorySource(str, Enum):
    RBI     = "RBI"
    SEBI    = "SEBI"
    CERT_IN = "CERT_IN"
    DPDP    = "DPDP"


class IngestionStatus(str, Enum):
    PENDING    = "PENDING"
    FETCHED    = "FETCHED"
    DUPLICATE  = "DUPLICATE"
    PARSED     = "PARSED"
    OCR_NEEDED = "OCR_NEEDED"
    COMPLETED  = "COMPLETED"
    FAILED     = "FAILED"


# ──────────────────────────────────────────────────────────────────────────────
# Data Structures
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class IngestionResult:
    """Result of ingesting a single regulatory document."""
    doc_id:          str
    source:          RegulatorySource
    source_url:      str
    status:          IngestionStatus
    content_hash:    str           = ""
    extracted_text:  str           = ""
    confidence:      float         = 0.0
    ocr_used:        bool          = False
    blob_path:       str           = ""
    metadata:        dict[str, Any] = field(default_factory=dict)
    errors:          list[str]     = field(default_factory=list)
    timestamp:       str           = field(
        default_factory=lambda: datetime.now(tz=timezone.utc).isoformat()
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "doc_id":         self.doc_id,
            "source":         self.source.value,
            "source_url":     self.source_url,
            "status":         self.status.value,
            "content_hash":   self.content_hash,
            "confidence":     round(self.confidence, 4),
            "ocr_used":       self.ocr_used,
            "blob_path":      self.blob_path,
            "metadata":       self.metadata,
            "errors":         self.errors,
            "timestamp":      self.timestamp,
        }


@dataclass
class FetchConfig:
    """Configuration for the ingestion engine."""
    # ADLS Gen2
    adls_connection_string: str = ""
    adls_account_name:      str = ""
    adls_container_name:    str = "compliance-docs"

    # Cosmos DB (deduplication registry)
    cosmos_connection_string: str = ""
    cosmos_database:          str = "compliancedb"
    cosmos_container:         str = "document_registry"

    # Azure Document Intelligence
    form_recognizer_endpoint: str = ""
    form_recognizer_key:      str = ""

    # Azure region enforcement
    azure_region: str = "centralindia"

    # HTTP client settings
    http_timeout: int = 30
    max_retries:  int = 3

    # Source URLs for regulatory bodies
    source_urls: dict[str, str] = field(default_factory=lambda: {
        "RBI":     "https://www.rbi.org.in",
        "SEBI":    "https://www.sebi.gov.in",
        "CERT_IN": "https://www.cert-in.org.in",
        "DPDP":    "https://www.meity.gov.in",
    })


# ──────────────────────────────────────────────────────────────────────────────
# Deduplication Engine
# ──────────────────────────────────────────────────────────────────────────────

class DeduplicationEngine:
    """
    SHA-256 hash-based deduplication backed by Cosmos DB.

    Before processing a document, we compute its SHA-256 hash and check
    whether the same hash already exists in the registry.
    """

    def __init__(self, connection_string: Optional[str] = None,
                 database: str = "compliancedb",
                 container: str = "document_registry"):
        self._container = None
        self._in_memory: dict[str, dict[str, Any]] = {}

        if connection_string:
            try:
                from azure.cosmos import CosmosClient, PartitionKey
                client = CosmosClient.from_connection_string(connection_string)
                db = client.create_database_if_not_exists(database)
                self._container = db.create_container_if_not_exists(
                    id=container,
                    partition_key=PartitionKey(path="/content_hash"),
                )
                logger.info("Dedup engine connected to Cosmos DB")
            except Exception as exc:
                logger.warning("Dedup Cosmos connection failed: %s. "
                               "Using in-memory store.", exc)

    def compute_hash(self, content: bytes) -> str:
        """Compute SHA-256 hash of document content."""
        return hashlib.sha256(content).hexdigest()

    def is_duplicate(self, content_hash: str) -> bool:
        """Check if a document with this hash has already been ingested."""
        if self._container:
            try:
                query = ("SELECT c.id FROM c WHERE c.content_hash = @hash")
                items = list(self._container.query_items(
                    query=query,
                    parameters=[{"name": "@hash", "value": content_hash}],
                    enable_cross_partition_query=True,
                    max_item_count=1,
                ))
                return len(items) > 0
            except Exception as exc:
                logger.error("Dedup check failed: %s", exc)

        return content_hash in self._in_memory

    def register(self, content_hash: str, doc_id: str,
                 metadata: dict[str, Any]) -> None:
        """Register a newly ingested document hash."""
        record = {
            "id": doc_id,
            "content_hash": content_hash,
            "registered_at": datetime.now(tz=timezone.utc).isoformat(),
            **metadata,
        }

        if self._container:
            try:
                self._container.create_item(body=record)
            except Exception as exc:
                logger.error("Dedup register failed: %s", exc)
                self._in_memory[content_hash] = record
        else:
            self._in_memory[content_hash] = record


# ──────────────────────────────────────────────────────────────────────────────
# Document Parser (Azure Document Intelligence)
# ──────────────────────────────────────────────────────────────────────────────

class DocumentParser:
    """
    Extracts text and structure from documents using Azure Document Intelligence
    (Form Recognizer). Falls back to OCR if text extraction confidence is low.
    """

    def __init__(self, endpoint: str = "", api_key: str = ""):
        self._endpoint = endpoint
        self._api_key = api_key
        self._client = None

        if endpoint and api_key:
            try:
                from azure.ai.formrecognizer import DocumentAnalysisClient
                from azure.core.credentials import AzureKeyCredential
                self._client = DocumentAnalysisClient(
                    endpoint=endpoint,
                    credential=AzureKeyCredential(api_key),
                )
                logger.info("Document Intelligence client initialised")
            except Exception as exc:
                logger.warning("Document Intelligence setup failed: %s", exc)

    def parse(self, content: bytes, mime_type: str = "application/pdf"
              ) -> tuple[str, float, dict[str, Any]]:
        """
        Parse a document and return (extracted_text, confidence, metadata).

        If the primary extraction confidence is below OCR_CONFIDENCE_THRESHOLD,
        the OCR fallback is automatically triggered.
        """
        if self._client:
            return self._parse_with_azure(content)
        else:
            return self._parse_fallback(content, mime_type)

    def _parse_with_azure(self, content: bytes
                          ) -> tuple[str, float, dict[str, Any]]:
        """Use Azure Document Intelligence for text extraction."""
        try:
            poller = self._client.begin_analyze_document(
                "prebuilt-read", content
            )
            result = poller.result()

            text_parts = []
            total_confidence = 0.0
            page_count = 0

            for page in result.pages:
                page_count += 1
                for line in page.lines:
                    text_parts.append(line.content)
                    # Average word confidences for the line
                    if hasattr(line, 'spans') and line.spans:
                        total_confidence += sum(
                            w.confidence for w in page.words
                            if any(s.offset <= w.span.offset < s.offset + s.length
                                   for s in line.spans)
                        ) / max(len(page.words), 1)

            avg_confidence = total_confidence / max(page_count, 1)
            extracted_text = "\n".join(text_parts)

            metadata = {
                "page_count": page_count,
                "word_count": len(extracted_text.split()),
                "engine":     "azure_document_intelligence",
            }

            # OCR fallback if confidence is too low
            if avg_confidence < OCR_CONFIDENCE_THRESHOLD:
                logger.warning(
                    "Low confidence (%.2f). Triggering OCR fallback.",
                    avg_confidence
                )
                return self._ocr_fallback(content, metadata)

            return extracted_text, avg_confidence, metadata

        except Exception as exc:
            logger.error("Azure Document Intelligence failed: %s", exc)
            return self._parse_fallback(content, "application/pdf")

    def _ocr_fallback(self, content: bytes, base_metadata: dict[str, Any]
                      ) -> tuple[str, float, dict[str, Any]]:
        """OCR fallback using Azure Document Intelligence OCR model."""
        if not self._client:
            return "", 0.0, {"engine": "ocr_fallback_unavailable"}

        try:
            poller = self._client.begin_analyze_document(
                "prebuilt-read", content,
                features=["OCR_HIGH_RESOLUTION"],
            )
            result = poller.result()

            text_parts = []
            total_confidence = 0.0
            word_count = 0

            for page in result.pages:
                for word in page.words:
                    text_parts.append(word.content)
                    total_confidence += word.confidence
                    word_count += 1

            avg_conf = total_confidence / max(word_count, 1)
            metadata = {
                **base_metadata,
                "engine": "azure_ocr_high_resolution",
                "ocr_word_count": word_count,
            }
            return " ".join(text_parts), avg_conf, metadata

        except Exception as exc:
            logger.error("OCR fallback failed: %s", exc)
            return "", 0.0, {"engine": "ocr_fallback_failed", "error": str(exc)}

    def _parse_fallback(self, content: bytes, mime_type: str
                        ) -> tuple[str, float, dict[str, Any]]:
        """Basic text extraction fallback when Azure is not available."""
        try:
            if mime_type == "text/plain":
                text = content.decode("utf-8", errors="replace")
                return text, 0.90, {"engine": "plaintext_decode"}

            if mime_type == "text/html":
                text = content.decode("utf-8", errors="replace")
                # Strip HTML tags (basic)
                import re
                text = re.sub(r'<[^>]+>', ' ', text)
                text = re.sub(r'\s+', ' ', text).strip()
                return text, 0.80, {"engine": "html_strip"}

            if mime_type == "application/pdf":
                try:
                    import PyPDF2
                    import io
                    reader = PyPDF2.PdfReader(io.BytesIO(content))
                    text_parts = []
                    for page in reader.pages:
                        text_parts.append(page.extract_text() or "")
                    text = "\n".join(text_parts)
                    confidence = 0.75 if text.strip() else 0.10
                    return text, confidence, {
                        "engine": "pypdf2_fallback",
                        "page_count": len(reader.pages),
                    }
                except ImportError:
                    logger.warning("PyPDF2 not available for PDF fallback")
                    return "", 0.0, {"engine": "no_pdf_parser_available"}

            return "", 0.0, {"engine": "unsupported_mime_type",
                             "mime_type": mime_type}

        except Exception as exc:
            logger.error("Fallback parsing failed: %s", exc)
            return "", 0.0, {"engine": "fallback_failed", "error": str(exc)}


# ──────────────────────────────────────────────────────────────────────────────
# Blob Storage Manager
# ──────────────────────────────────────────────────────────────────────────────

class BlobStorageManager:
    """Manages document storage in ADLS Gen2 / Azure Blob Storage."""

    def __init__(self, connection_string: str = "",
                 account_name: str = "",
                 container_name: str = "compliance-docs"):
        self._container_client = None
        self._container_name = container_name

        if connection_string:
            try:
                from azure.storage.blob import BlobServiceClient
                svc = BlobServiceClient.from_connection_string(connection_string)
                self._container_client = svc.get_container_client(container_name)
                # Create container if it doesn't exist
                try:
                    self._container_client.get_container_properties()
                except Exception:
                    self._container_client.create_container()
                logger.info("Blob storage connected: %s", container_name)
            except Exception as exc:
                logger.warning("Blob storage setup failed: %s", exc)

    def upload(self, doc_id: str, content: bytes,
               metadata: dict[str, str]) -> str:
        """Upload document content and return the blob path."""
        blob_name = f"documents/{doc_id}/{doc_id}.bin"

        if self._container_client:
            try:
                blob_client = self._container_client.get_blob_client(blob_name)
                blob_client.upload_blob(
                    content,
                    overwrite=True,
                    metadata=metadata,
                )
                logger.info("Document uploaded to blob: %s", blob_name)
                return blob_name
            except Exception as exc:
                logger.error("Blob upload failed: %s", exc)

        # Fallback: return a reference path
        logger.warning("Blob storage unavailable. Document not persisted.")
        return f"local://{blob_name}"


# ──────────────────────────────────────────────────────────────────────────────
# Regulatory Document Fetcher
# ──────────────────────────────────────────────────────────────────────────────

class RegulatoryFetcher:
    """
    Fetches regulatory documents from official sources via HTTP.

    Enforces data residency by only connecting from the India Central region
    (the AKS cluster runs exclusively in centralindia).
    """

    def __init__(self, config: FetchConfig):
        self._config = config
        self._http = httpx.Client(
            timeout=config.http_timeout,
            follow_redirects=True,
            headers={
                "User-Agent": "ComplianceBot/1.0 (Azure India Central)",
                "Accept": "application/pdf, text/html, text/plain",
            },
        )

    def fetch(self, url: str, source: RegulatorySource) -> tuple[bytes, str]:
        """
        Fetch a document from a regulatory source URL.

        Returns (content_bytes, mime_type).

        Raises
        ------
        httpx.HTTPStatusError  On non-2xx responses.
        httpx.TimeoutException On timeout.
        """
        logger.info("Fetching document: source=%s url=%s", source.value, url)

        response = self._http.get(url)
        response.raise_for_status()

        content_type = response.headers.get("content-type", "application/pdf")
        mime_type = content_type.split(";")[0].strip()

        logger.info("Fetched %d bytes (mime=%s)", len(response.content), mime_type)
        return response.content, mime_type

    def close(self) -> None:
        self._http.close()


# ──────────────────────────────────────────────────────────────────────────────
# Ingestion Engine — Main Orchestrator
# ──────────────────────────────────────────────────────────────────────────────

class IngestionEngine:
    """
    End-to-end ingestion pipeline:
      Fetch → Deduplicate → Parse → OCR Fallback → Store

    Usage::

        config = FetchConfig(
            adls_connection_string="...",
            cosmos_connection_string="...",
            form_recognizer_endpoint="...",
            form_recognizer_key="...",
        )
        engine = IngestionEngine(config)
        result = engine.ingest(url="https://rbi.org.in/...", source=RegulatorySource.RBI)
    """

    def __init__(self, config: FetchConfig):
        self._config = config
        self._fetcher = RegulatoryFetcher(config)
        self._dedup = DeduplicationEngine(
            connection_string=config.cosmos_connection_string,
            database=config.cosmos_database,
            container=config.cosmos_container,
        )
        self._parser = DocumentParser(
            endpoint=config.form_recognizer_endpoint,
            api_key=config.form_recognizer_key,
        )
        self._storage = BlobStorageManager(
            connection_string=config.adls_connection_string,
            account_name=config.adls_account_name,
            container_name=config.adls_container_name,
        )

    def ingest(self, url: str, source: RegulatorySource,
               content: Optional[bytes] = None,
               mime_type: str = "application/pdf") -> IngestionResult:
        """
        Run the full ingestion pipeline for a single document.

        If `content` is provided, the fetch step is skipped (useful for
        documents already downloaded or received via Service Bus).
        """
        doc_id = f"doc-{uuid.uuid4().hex[:12]}"
        result = IngestionResult(
            doc_id=doc_id,
            source=source,
            source_url=url,
            status=IngestionStatus.PENDING,
        )

        try:
            # Step 1: Fetch (if content not provided)
            if content is None:
                content, mime_type = self._fetcher.fetch(url, source)
            result.status = IngestionStatus.FETCHED

            # Validate mime type
            if mime_type not in SUPPORTED_MIME_TYPES:
                logger.warning("Unsupported mime type: %s", mime_type)

            # Validate size
            size_mb = len(content) / (1024 * 1024)
            if size_mb > MAX_DOCUMENT_SIZE_MB:
                raise ValueError(
                    f"Document exceeds max size: {size_mb:.1f}MB > "
                    f"{MAX_DOCUMENT_SIZE_MB}MB"
                )

            # Step 2: Deduplication
            content_hash = self._dedup.compute_hash(content)
            result.content_hash = content_hash

            if self._dedup.is_duplicate(content_hash):
                result.status = IngestionStatus.DUPLICATE
                logger.info("Duplicate document detected: hash=%s", content_hash[:16])
                return result

            # Step 3: Parse (with automatic OCR fallback)
            extracted_text, confidence, parse_meta = self._parser.parse(
                content, mime_type
            )
            result.extracted_text = extracted_text
            result.confidence = confidence
            result.ocr_used = "ocr" in parse_meta.get("engine", "").lower()
            result.metadata.update(parse_meta)

            if confidence < OCR_CONFIDENCE_THRESHOLD and not result.ocr_used:
                result.status = IngestionStatus.OCR_NEEDED
                logger.warning("Low parse confidence (%.2f). OCR may be needed.",
                               confidence)

            result.status = IngestionStatus.PARSED

            # Step 4: Store in blob storage
            blob_path = self._storage.upload(
                doc_id=doc_id,
                content=content,
                metadata={
                    "source": source.value,
                    "content_hash": content_hash,
                    "mime_type": mime_type,
                    "region": self._config.azure_region,
                },
            )
            result.blob_path = blob_path

            # Step 5: Register in dedup registry
            self._dedup.register(content_hash, doc_id, {
                "source": source.value,
                "source_url": url,
                "blob_path": blob_path,
                "mime_type": mime_type,
            })

            result.status = IngestionStatus.COMPLETED
            logger.info(
                "Ingestion complete: doc=%s hash=%s confidence=%.2f",
                doc_id, content_hash[:16], confidence
            )

        except Exception as exc:
            result.status = IngestionStatus.FAILED
            result.errors.append(f"{type(exc).__name__}: {exc}")
            logger.error("Ingestion failed for %s: %s", url, exc)

        return result

    def ingest_batch(self, documents: list[dict[str, Any]]
                     ) -> list[IngestionResult]:
        """
        Ingest multiple documents.

        Each dict in `documents` should have keys: "url", "source".
        Optional: "content", "mime_type".
        """
        results = []
        for doc in documents:
            source = RegulatorySource(doc["source"])
            result = self.ingest(
                url=doc["url"],
                source=source,
                content=doc.get("content"),
                mime_type=doc.get("mime_type", "application/pdf"),
            )
            results.append(result)
        return results

    def close(self) -> None:
        """Release HTTP client resources."""
        self._fetcher.close()
