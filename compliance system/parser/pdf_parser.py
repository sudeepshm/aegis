"""
parser/pdf_parser.py
====================
Embedding and vector database indexing engine.

Chunking and entity extraction are handled by ``parser.chunker``.
This module provides:
  - EmbeddingEngine   → Generate embeddings via Azure OpenAI
  - VectorIndexWriter → Index into Azure AI Search (Cognitive Search)
  - ParsePipeline     → Orchestrates: Chunk → Enrich → Embed → Index

Dependencies
────────────
  pip install azure-search-documents openai tiktoken
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any, Optional

# Import chunking classes from the dedicated chunker module
from parser.chunker import (
    Entity,
    SemanticChunk,
    ParseResult,
    EntityExtractor,
    SemanticChunker,
    DEFAULT_CHUNK_SIZE,
    DEFAULT_CHUNK_OVERLAP,
)

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────────
# Embedding Engine
# ──────────────────────────────────────────────────────────────────────────────

class EmbeddingEngine:
    """
    Generates vector embeddings using Azure OpenAI.

    Uses the text-embedding-3-small model by default.
    Falls back to a simple hash-based pseudo-embedding when Azure OpenAI is
    unavailable.
    """

    def __init__(self, api_key: str = "", endpoint: str = "",
                 deployment: str = "text-embedding-3-small",
                 dimensions: int = 1536):
        self._api_key = api_key
        self._endpoint = endpoint
        self._deployment = deployment
        self._dimensions = dimensions
        self._client = None

        if api_key and endpoint:
            try:
                from openai import AzureOpenAI
                self._client = AzureOpenAI(
                    api_key=api_key,
                    api_version="2024-02-01",
                    azure_endpoint=endpoint,
                )
                logger.info("Embedding engine connected to Azure OpenAI")
            except Exception as exc:
                logger.warning("Azure OpenAI setup failed: %s", exc)

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Generate embeddings for a list of texts."""
        if self._client:
            return self._embed_azure(texts)
        return self._embed_fallback(texts)

    def _embed_azure(self, texts: list[str]) -> list[list[float]]:
        """Generate embeddings via Azure OpenAI."""
        try:
            response = self._client.embeddings.create(
                input=texts,
                model=self._deployment,
            )
            return [item.embedding for item in response.data]
        except Exception as exc:
            logger.error("Azure OpenAI embedding failed: %s", exc)
            return self._embed_fallback(texts)

    def _embed_fallback(self, texts: list[str]) -> list[list[float]]:
        """
        Hash-based pseudo-embedding fallback.
        Produces deterministic but non-semantic embeddings.
        Only for dev/testing — NOT for production similarity search.
        """
        embeddings = []
        for text in texts:
            h = hashlib.sha256(text.encode("utf-8")).digest()
            # Expand hash to fill dimensions
            expanded = list(h) * (self._dimensions // len(h) + 1)
            vec = [float(b) / 255.0 for b in expanded[:self._dimensions]]
            embeddings.append(vec)
        return embeddings

    def embed_chunks(self, chunks: list[SemanticChunk]) -> list[SemanticChunk]:
        """Generate and attach embeddings to chunks."""
        if not chunks:
            return chunks

        texts = [c.text for c in chunks]
        embeddings = self.embed(texts)

        for chunk, emb in zip(chunks, embeddings):
            chunk.embedding = emb

        logger.info("Embedded %d chunks (dim=%d)", len(chunks), self._dimensions)
        return chunks


# ──────────────────────────────────────────────────────────────────────────────
# Vector Index Writer (Azure AI Search)
# ──────────────────────────────────────────────────────────────────────────────

class VectorIndexWriter:
    """
    Writes enriched chunks with embeddings to Azure AI Search.

    Creates and maintains a vector search index for semantic retrieval
    of regulatory document chunks.
    """

    def __init__(self, endpoint: str = "", api_key: str = "",
                 index_name: str = "compliance-chunks"):
        self._index_name = index_name
        self._search_client = None
        self._index_client = None

        if endpoint and api_key:
            try:
                from azure.search.documents import SearchClient
                from azure.search.documents.indexes import SearchIndexClient
                from azure.core.credentials import AzureKeyCredential

                credential = AzureKeyCredential(api_key)
                self._search_client = SearchClient(
                    endpoint=endpoint,
                    index_name=index_name,
                    credential=credential,
                )
                self._index_client = SearchIndexClient(
                    endpoint=endpoint,
                    credential=credential,
                )
                logger.info("Vector index writer connected: %s", index_name)
            except Exception as exc:
                logger.warning("Azure AI Search setup failed: %s", exc)

    def index_chunks(self, chunks: list[SemanticChunk]) -> int:
        """
        Upload chunks to the search index.

        Returns the number of successfully indexed documents.
        """
        if not self._search_client:
            logger.warning("Search client not available. Chunks not indexed.")
            return 0

        documents = []
        for chunk in chunks:
            doc = {
                "id":           chunk.chunk_id,
                "doc_id":       chunk.doc_id,
                "text":         chunk.text,
                "section":      chunk.section,
                "chunk_index":  chunk.chunk_index,
                "token_count":  chunk.token_count,
                "content_hash": chunk.content_hash,
                "entities":     json.dumps([e.to_dict() for e in chunk.entities]),
                "metadata":     json.dumps(chunk.metadata),
            }
            if chunk.embedding:
                doc["embedding"] = chunk.embedding
            documents.append(doc)

        try:
            result = self._search_client.upload_documents(documents=documents)
            success_count = sum(1 for r in result if r.succeeded)
            logger.info("Indexed %d/%d chunks into '%s'",
                        success_count, len(chunks), self._index_name)
            return success_count
        except Exception as exc:
            logger.error("Vector indexing failed: %s", exc)
            return 0


# ──────────────────────────────────────────────────────────────────────────────
# Full Parse Pipeline
# ──────────────────────────────────────────────────────────────────────────────

class ParsePipeline:
    """
    Orchestrates the full parse pipeline:
      Chunk → Enrich → Embed → Index

    Usage::

        pipeline = ParsePipeline(aoai_key="...", aoai_endpoint="...",
                                 search_endpoint="...", search_key="...")
        result = pipeline.run(doc_id="doc-001", text="...")
    """

    def __init__(
        self,
        aoai_key: str = "",
        aoai_endpoint: str = "",
        aoai_deployment: str = "text-embedding-3-small",
        search_endpoint: str = "",
        search_key: str = "",
        search_index: str = "compliance-chunks",
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        chunk_overlap: int = DEFAULT_CHUNK_OVERLAP,
    ):
        self._chunker = SemanticChunker(chunk_size, chunk_overlap)
        self._embedder = EmbeddingEngine(
            api_key=aoai_key,
            endpoint=aoai_endpoint,
            deployment=aoai_deployment,
        )
        self._indexer = VectorIndexWriter(
            endpoint=search_endpoint,
            api_key=search_key,
            index_name=search_index,
        )

    def run(self, doc_id: str, text: str,
            metadata: Optional[dict[str, Any]] = None) -> ParseResult:
        """Run the complete parse pipeline."""
        # Step 1: Semantic chunking + entity enrichment
        result = self._chunker.chunk(doc_id, text, metadata)
        logger.info("Chunked doc %s: %d chunks, %d tokens, %d entities",
                     doc_id, len(result.chunks), result.total_tokens,
                     result.total_entities)

        # Step 2: Generate embeddings
        if result.chunks:
            result.chunks = self._embedder.embed_chunks(result.chunks)

        # Step 3: Index into vector database
        if result.chunks:
            indexed = self._indexer.index_chunks(result.chunks)
            logger.info("Indexed %d chunks for doc %s", indexed, doc_id)

        return result
