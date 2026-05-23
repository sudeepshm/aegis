"""
ingestion/fetch_sebi.py
-----------------------
SEBI Notifications Ingestion Engine — Project Aegis Phase 2

Pipeline:
  1. Scrape SEBI circulars HTML page → extract PDF links
  2. Deduplicate via SHA-256 (Redis primary, Azure Table fallback)
  3. Download PDFs → upload to Azure Blob Storage
       /regulatory-store/sebi/{year}/{month}/{document_id}.pdf
  4. Parse PDFs with Azure Document Intelligence (Tesseract OCR fallback)
  5. Normalize to standard JSON schema (identical to RBI output)
  6. DLQ (Azure Service Bus) for failed documents

Environment variables (reuses same Azure infra as fetch_rbi.py):
  SEBI_FEED_URL                   – SEBI circulars page URL
  AZURE_STORAGE_CONN_STR          – Blob Storage connection string
  AZURE_DOC_INTEL_ENDPOINT        – Document Intelligence endpoint
  AZURE_DOC_INTEL_KEY             – Document Intelligence API key
  AZURE_SERVICEBUS_CONN_STR       – Service Bus connection string
  AZURE_SERVICEBUS_DLQ_QUEUE      – DLQ queue name (reused)
  AZURE_HASH_TABLE_CONN_STR       – Table Storage connection string
  AZURE_HASH_TABLE_NAME           – Table name for dedup registry
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import uuid
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ingestion.base_fetcher import (
    BaseRegulatoryFetcher,
    RawFeedEntry,
    ExtractedContent,
    NormalizedDocument,
    normalize,
    document_to_dict,
    _env,
    _utcnow_iso,
    BLOB_CONTAINER,
    AZURE_REGION,
    PDF_DOWNLOAD_TIMEOUT,
    PDF_CHUNK_SIZE,
)

# Reuse Azure infrastructure classes from fetch_rbi
from ingestion.fetch_rbi import (
    HashRegistry,
    BlobStore,
    DocumentParser,
    DeadLetterQueue,
)

# Project Aegis — Redis-backed deduplication (Phase 1)
try:
    from core.redis_client import get_redis
    _REDIS_AVAILABLE = True
except ImportError:
    _REDIS_AVAILABLE = False

logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

REGULATOR = "sebi"

DEFAULT_SEBI_FEED_URL = (
    "https://www.sebi.gov.in/sebiweb/home/HomeAction.do"
    "?doListing=yes&sid=4&ssid=&smid=0"
)

SEBI_BASE_URL = "https://www.sebi.gov.in"

# HTTP
SEBI_SCRAPE_TIMEOUT = 30


# ---------------------------------------------------------------------------
# SEBI HTML Feed Scraper
# ---------------------------------------------------------------------------


class SEBIFeedFetcher:
    """
    Scrapes SEBI's circulars/orders HTML listing page.

    Strategy:
      1. GET the SEBI circulars listing page
      2. Parse with BeautifulSoup
      3. Extract <a> tags pointing to .pdf files
      4. Build RawFeedEntry objects from title + link + date
    """

    def __init__(self, feed_url: str) -> None:
        self._feed_url = feed_url

    def fetch_entries(self) -> list[RawFeedEntry]:
        logger.info("Scraping SEBI circulars page: %s", self._feed_url)
        resp = requests.get(self._feed_url, timeout=SEBI_SCRAPE_TIMEOUT)
        resp.raise_for_status()

        soup = BeautifulSoup(resp.content, "lxml")
        entries: list[RawFeedEntry] = []

        # SEBI listing pages typically contain <a> tags with PDF links
        # inside table rows or list items with date information
        for row in soup.find_all("div", class_="list-item"):
            self._parse_list_item(row, entries)

        # Fallback: scan for any <a> tags linking to .pdf
        if not entries:
            for link in soup.find_all("a", href=True):
                href = link.get("href", "")
                if href.lower().endswith(".pdf"):
                    self._parse_pdf_link(link, entries)

        # Final fallback: look in table rows
        if not entries:
            for row in soup.find_all("tr"):
                links = row.find_all("a", href=True)
                for link in links:
                    href = link.get("href", "")
                    if href.lower().endswith(".pdf"):
                        self._parse_table_row(row, link, entries)

        logger.info("Scraped %d entries from SEBI page.", len(entries))
        return entries

    def _parse_list_item(
        self, row: Any, entries: list[RawFeedEntry]
    ) -> None:
        """Parse a SEBI-style list-item div."""
        link_tag = row.find("a", href=True)
        if not link_tag:
            return

        href = link_tag.get("href", "")
        if not href:
            return

        # Resolve relative URLs
        if not href.startswith("http"):
            href = urljoin(SEBI_BASE_URL, href)

        title = link_tag.get_text(strip=True) or "Untitled SEBI Circular"

        # Try to find a date in the row
        date_text = ""
        date_span = row.find("span", class_="date") or row.find("span", class_="list-date")
        if date_span:
            date_text = date_span.get_text(strip=True)

        published = self._parse_date(date_text)

        entries.append(
            RawFeedEntry(
                entry_id=href,
                title=title[:300],
                link=href,
                published=published,
                summary="",
            )
        )

    def _parse_pdf_link(
        self, link: Any, entries: list[RawFeedEntry]
    ) -> None:
        """Parse a standalone <a> tag pointing to a PDF."""
        href = link.get("href", "")
        if not href.startswith("http"):
            href = urljoin(SEBI_BASE_URL, href)

        title = link.get_text(strip=True) or "Untitled SEBI Document"
        published = datetime.now(timezone.utc)

        entries.append(
            RawFeedEntry(
                entry_id=href,
                title=title[:300],
                link=href,
                published=published,
                summary="",
            )
        )

    def _parse_table_row(
        self, row: Any, link: Any, entries: list[RawFeedEntry]
    ) -> None:
        """Parse a table row containing a PDF link."""
        href = link.get("href", "")
        if not href.startswith("http"):
            href = urljoin(SEBI_BASE_URL, href)

        title = link.get_text(strip=True) or "Untitled SEBI Document"

        # Try to find date in sibling <td> cells
        cells = row.find_all("td")
        date_text = ""
        for cell in cells:
            text = cell.get_text(strip=True)
            if re.match(r"\d{1,2}[-/]\d{1,2}[-/]\d{2,4}", text):
                date_text = text
                break

        published = self._parse_date(date_text)

        entries.append(
            RawFeedEntry(
                entry_id=href,
                title=title[:300],
                link=href,
                published=published,
                summary="",
            )
        )

    @staticmethod
    def _parse_date(text: str) -> datetime:
        """Best-effort date parsing from SEBI's inconsistent formats."""
        if not text:
            return datetime.now(timezone.utc)

        for fmt in (
            "%d-%m-%Y", "%d/%m/%Y", "%d %b %Y", "%d %B %Y",
            "%b %d, %Y", "%B %d, %Y", "%Y-%m-%d",
        ):
            try:
                dt = datetime.strptime(text.strip(), fmt)
                return dt.replace(tzinfo=timezone.utc)
            except ValueError:
                continue

        return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# SEBI Ingestion Engine
# ---------------------------------------------------------------------------


class SEBIIngestionEngine(BaseRegulatoryFetcher):
    """
    Orchestrates the full SEBI circular ingestion pipeline.

    Mirrors ``RBIIngestionEngine`` architecture — same dedup, same DLQ,
    same Blob paths, same ``NormalizedDocument`` output schema. Only the
    feed parsing strategy differs (HTML scrape vs RSS).

    Usage
    -----
    engine = SEBIIngestionEngine()
    results = engine.run()
    """

    REGULATOR = "sebi"

    def __init__(self, feed_url: str | None = None) -> None:
        self._feed_url = feed_url or _env(
            "SEBI_FEED_URL", DEFAULT_SEBI_FEED_URL
        )
        self._fetcher = SEBIFeedFetcher(self._feed_url)
        self._registry = HashRegistry(
            redis_client=get_redis() if _REDIS_AVAILABLE else None,
            namespace=REGULATOR,
        )
        self._blob_store = BlobStore()
        self._parser = DocumentParser()
        self._dlq = DeadLetterQueue()

    # ------------------------------------------------------------------
    # BaseRegulatoryFetcher interface
    # ------------------------------------------------------------------

    def fetch_entries(self) -> list[RawFeedEntry]:
        return self._fetcher.fetch_entries()

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    def run(self) -> list[dict[str, Any]]:
        """Execute one full SEBI ingestion cycle."""
        logger.info(
            "=== SEBI Ingestion Engine starting (region: %s) ===",
            AZURE_REGION,
        )
        entries = self.fetch_entries()

        ingested: list[dict[str, Any]] = []
        skipped = 0
        failed = 0

        for entry in entries:
            if self._registry.exists(entry.sha256):
                logger.debug(
                    "Skipping duplicate: %s (sha256=%s)",
                    entry.title, entry.sha256[:12],
                )
                skipped += 1
                continue

            result = self._process_entry(entry)
            if result is not None:
                ingested.append(result)
            else:
                failed += 1

        logger.info(
            "=== SEBI ingestion cycle complete — "
            "ingested=%d | skipped=%d | failed=%d ===",
            len(ingested), skipped, failed,
        )
        return ingested

    # ------------------------------------------------------------------
    # Per-entry processing
    # ------------------------------------------------------------------

    def _process_entry(self, entry: RawFeedEntry) -> dict[str, Any] | None:
        document_id = str(uuid.uuid4())
        logger.info("Processing [%s] %s", document_id[:8], entry.title)

        # Step 1 — Download PDF
        try:
            pdf_bytes = self.download_pdf(entry.link)
        except Exception as exc:
            error_msg = f"PDF download failed: {exc}"
            logger.error(error_msg)
            self._dlq.send(document_id, entry.link, error_msg)
            return None

        # Step 2 — Upload raw PDF to Blob Storage
        try:
            blob_path = self._blob_store.upload_pdf(
                pdf_bytes, document_id, entry.published
            )
        except Exception as exc:
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

        # Step 4 — Normalize (using shared schema)
        doc = normalize(entry, blob_path, content, regulator=REGULATOR)

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
# CLI entrypoint (for local testing)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    feed_url = sys.argv[1] if len(sys.argv) > 1 else None
    engine = SEBIIngestionEngine(feed_url=feed_url)
    results = engine.run()

    output_path = os.path.join(
        tempfile.gettempdir(), "sebi_ingestion_results.json"
    )
    with open(output_path, "w", encoding="utf-8") as fh:
        json.dump(results, fh, indent=2, ensure_ascii=False)

    print(f"Ingested {len(results)} documents. Output: {output_path}")
