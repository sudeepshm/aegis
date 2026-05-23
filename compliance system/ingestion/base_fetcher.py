"""
ingestion/base_fetcher.py
=========================
Abstract base class for regulatory document fetchers.

Provides the shared download → parse → normalize → upload template method
``run()`` that both ``RBIIngestionEngine`` and ``SEBIIngestionEngine``
inherit.  Subclasses implement only:

    * ``REGULATOR``     — class-level string constant (``"rbi"`` / ``"sebi"``)
    * ``fetch_entries`` — regulator-specific feed/page scraping

Everything else (PDF download, Azure Document Intelligence parsing, Azure Blob
upload, Redis-backed dedup, DLQ, normalisation) is handled once in this base.
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
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, ClassVar
from urllib.parse import urlparse

import requests

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Shared constants
# ---------------------------------------------------------------------------

BLOB_CONTAINER = "regulatory-store"
AZURE_REGION = "indiacentral"

FEED_TIMEOUT = 30
PDF_DOWNLOAD_TIMEOUT = 60
PDF_CHUNK_SIZE = 1024 * 256  # 256 KB

# ---------------------------------------------------------------------------
# Shared data models
# ---------------------------------------------------------------------------


@dataclass
class RawFeedEntry:
    """A single entry parsed from a regulatory feed (RSS, HTML scrape, etc.)."""

    entry_id: str          # feed-provided GUID or link
    title: str
    link: str              # URL to the notification / PDF
    published: datetime
    summary: str = ""
    sha256: str = field(init=False)

    def __post_init__(self) -> None:
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
    parsed = urlparse(url)
    name = os.path.basename(parsed.path) or "document"
    name = re.sub(r"[^\w.\-]", "_", name)
    return name if name.endswith(".pdf") else f"{name}.pdf"


def normalize(
    entry: RawFeedEntry,
    blob_path: str,
    content: ExtractedContent,
    regulator: str,
) -> NormalizedDocument:
    """Produce the canonical JSON-serializable document schema."""
    return NormalizedDocument(
        document_id=str(uuid.uuid4()),
        regulator=regulator,
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
# Abstract Base Class
# ---------------------------------------------------------------------------


class BaseRegulatoryFetcher(ABC):
    """
    Template-method base class for regulatory ingestion engines.

    Subclasses set ``REGULATOR`` (``"rbi"`` / ``"sebi"``) and implement
    ``fetch_entries()`` to parse their specific feed format.  Everything
    else — download, parse, normalise, deduplicate, upload — is shared.
    """

    REGULATOR: ClassVar[str]  # must be overridden by subclass

    # ------------------------------------------------------------------
    # Abstract method — subclass-specific
    # ------------------------------------------------------------------

    @abstractmethod
    def fetch_entries(self) -> list[RawFeedEntry]:
        """Fetch raw feed entries from the regulatory source."""
        ...

    # ------------------------------------------------------------------
    # Concrete shared methods
    # ------------------------------------------------------------------

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

    @staticmethod
    def compute_sha256(data: bytes) -> str:
        """SHA-256 hex digest of raw bytes."""
        return hashlib.sha256(data).hexdigest()
