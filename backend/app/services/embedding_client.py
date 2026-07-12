"""Embedding client for gemini-embedding-2 (multimodal, GA).

Key differences from gemini-embedding-001 this code accounts for:
1. No ``task_type`` parameter — task instructions are prepended into the
   input text itself as a prefix string.
2. Passing multiple strings in one ``contents`` call returns a SINGLE
   aggregated embedding, not one-per-string. To get one vector per
   input (which is what a document-chunk index needs), call
   ``embed_content`` once per string.

Usage
-----
    from app.services.embedding_client import GeminiEmbeddingClient

    client = GeminiEmbeddingClient()

    # Indexing document chunks:
    vectors = client.embed_documents(["Revenue grew 15%...", "Q3 highlights..."])

    # Querying:
    query_vec = client.embed_query("revenue growth metrics")
"""
from __future__ import annotations

import logging

from app.core.config import settings

log = logging.getLogger(__name__)


class TaskType:
    """Task instruction prefixes for gemini-embedding-2.

    NOTE: verify these exact prefix strings against the current
    'Task types with Embeddings 2' section of ai.google.dev/gemini-api/docs/embeddings
    before relying on them in production — Google's docs give worked
    examples per task rather than one fixed template, and the exact
    wording matters for embedding quality.
    """
    RETRIEVAL_DOCUMENT = "search result"   # use when indexing documents/chunks
    RETRIEVAL_QUERY    = "search query"    # use when embedding a user query
    SEMANTIC_SIMILARITY = "similarity"
    CLASSIFICATION     = "classification"
    CLUSTERING         = "clustering"


class GeminiEmbeddingClient:
    """Embedding client for gemini-embedding-2."""

    MODEL = "gemini-embedding-2"

    def __init__(self, output_dimensionality: int = 768) -> None:
        from google import genai  # type: ignore[import]
        if not settings.gemini_api_key:
            raise RuntimeError(
                "GEMINI_API_KEY is not set. Required for GeminiEmbeddingClient."
            )
        self._client = genai.Client(api_key=settings.gemini_api_key)
        # 768 recommended for storage efficiency; bump to 1536/3072 if
        # retrieval quality benchmarks demand it.
        # IMPORTANT: this must match the dimensionality your Qdrant
        # collection was created with — changing it requires a re-index.
        self._output_dim = output_dimensionality

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of document chunks for indexing.

        One embed_content call per text — NOT one call with all texts —
        because gemini-embedding-2 aggregates multi-input calls into a
        single vector rather than returning one per input.
        """
        vectors: list[list[float]] = []
        for text in texts:
            formatted = self._format_input(text, TaskType.RETRIEVAL_DOCUMENT, role="document")
            try:
                response = self._client.models.embed_content(
                    model=self.MODEL,
                    contents=formatted,
                    config={"output_dimensionality": self._output_dim},
                )
                vectors.append(list(response.embeddings[0].values))
            except Exception as exc:
                log.error("gemini-embedding-2 document embed failed: %s", exc)
                raise
        return vectors

    def embed_query(self, text: str) -> list[float]:
        """Embed a single query string for similarity search against the index."""
        formatted = self._format_input(text, TaskType.RETRIEVAL_QUERY, role="query")
        try:
            response = self._client.models.embed_content(
                model=self.MODEL,
                contents=formatted,
                config={"output_dimensionality": self._output_dim},
            )
            return list(response.embeddings[0].values)
        except Exception as exc:
            log.error("gemini-embedding-2 query embed failed: %s", exc)
            raise

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _format_input(self, text: str, task: str, role: str = "document") -> str:
        """Prepend the task instruction prefix required by gemini-embedding-2.

        ``role`` distinguishes asymmetric retrieval: documents get indexed
        under one framing, queries embedded under another, so a query
        and its matching document land close together in vector space.
        """
        return f"task: {task} | {role}: {text}"
