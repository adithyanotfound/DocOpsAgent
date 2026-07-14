"""Knowledge Base Retrieval Service.

Embeds KB chunks into a dedicated Qdrant collection ('knowledge_chunks')
and retrieves relevant passages for the document generation pipeline.

Uses a separate Qdrant collection from the existing 'document_blocks'
so document structure index and KB content index remain isolated.

Falls back to SQLite keyword search when Qdrant is not configured.
"""
from __future__ import annotations

import logging
import re
import uuid
from typing import Any

from app.core.config import settings

log = logging.getLogger(__name__)

KB_COLLECTION = "knowledge_chunks"
STOPWORDS = {
    "a", "all", "an", "and", "are", "as", "at", "be", "by", "for",
    "from", "has", "he", "in", "is", "it", "its", "of", "on", "or",
    "that", "the", "this", "to", "was", "were", "will", "with",
}


class KBRetrievalService:
    """Manages embedding and retrieval of knowledge base chunks."""

    def __init__(self, embed_client=None) -> None:
        self._embed_client = embed_client

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def index_document(
        self,
        workspace_id: str,
        doc_id: str,
        chunks: list[dict],
    ) -> None:
        """Embed and upsert chunks into Qdrant (or no-op if not configured)."""
        if not self._can_use_qdrant():
            log.info("Qdrant not configured — KB chunks stored in SQLite only.")
            return
        try:
            self._upsert_chunks(workspace_id, doc_id, chunks)
        except Exception as exc:
            log.warning("KB indexing failed (Qdrant): %s", exc)

    def retrieve(
        self,
        workspace_id: str,
        query: str,
        chunks_from_db: list[dict],
        limit: int = 15,
    ) -> list[dict]:
        """Retrieve the most relevant KB chunks for a generation query.

        Args:
            workspace_id: The workspace whose KB to search.
            query: User's generation request.
            chunks_from_db: All chunks for the workspace from SQLite
                            (used as fallback and for re-ranking).
            limit: Number of top chunks to return.
        """
        if self._can_use_qdrant():
            try:
                return self._retrieve_semantic(workspace_id, query, chunks_from_db, limit)
            except Exception as exc:
                log.warning("Qdrant KB retrieval failed: %s. Falling back.", exc)

        return self._retrieve_local(query, chunks_from_db, limit)

    def delete_document(self, workspace_id: str, doc_id: str) -> None:
        """Remove all Qdrant vectors for a specific KB document."""
        if not self._can_use_qdrant():
            return
        try:
            from qdrant_client.models import Filter, FieldCondition, MatchValue
            qdrant = self._get_qdrant_client()
            qdrant.delete(
                collection_name=KB_COLLECTION,
                points_selector=Filter(
                    must=[
                        FieldCondition(key="workspace_id", match=MatchValue(value=workspace_id)),
                        FieldCondition(key="doc_id", match=MatchValue(value=doc_id)),
                    ]
                ),
            )
        except Exception as exc:
            if "Not found" not in str(exc) and "404" not in str(exc):
                log.warning("KB document delete from Qdrant failed: %s", exc)

    def delete_workspace_kb(self, workspace_id: str) -> None:
        """Remove all KB vectors for a workspace from Qdrant."""
        if not self._can_use_qdrant():
            return
        try:
            from qdrant_client.models import Filter, FieldCondition, MatchValue
            qdrant = self._get_qdrant_client()
            qdrant.delete(
                collection_name=KB_COLLECTION,
                points_selector=Filter(
                    must=[
                        FieldCondition(key="workspace_id", match=MatchValue(value=workspace_id)),
                    ]
                ),
            )
        except Exception as exc:
            if "Not found" not in str(exc) and "404" not in str(exc):
                log.warning("KB workspace delete from Qdrant failed: %s", exc)

    # ------------------------------------------------------------------
    # Qdrant internals
    # ------------------------------------------------------------------

    def _can_use_qdrant(self) -> bool:
        return bool(settings.qdrant_url and (settings.gemini_api_key or settings.openai_api_key))

    def _get_qdrant_client(self):
        from qdrant_client import QdrantClient
        return QdrantClient(url=settings.qdrant_url, api_key=settings.qdrant_api_key or None)

    def _get_embedding_client(self):
        if self._embed_client:
            return self._embed_client
        if settings.llm_provider == "gemini":
            from app.services.embedding_client import GeminiEmbeddingClient
            return GeminiEmbeddingClient()
        from app.services.embedding_client import OpenAIEmbeddingClient
        return OpenAIEmbeddingClient()

    def _ensure_collection(self, qdrant, vector_size: int) -> None:
        from qdrant_client.models import Distance, VectorParams, PayloadSchemaType
        existing = {col.name for col in qdrant.get_collections().collections}
        if KB_COLLECTION not in existing:
            try:
                qdrant.create_collection(
                    collection_name=KB_COLLECTION,
                    vectors_config=VectorParams(size=vector_size, distance=Distance.COSINE),
                )
                qdrant.create_payload_index(
                    collection_name=KB_COLLECTION,
                    field_name="workspace_id",
                    field_schema=PayloadSchemaType.KEYWORD,
                )
                qdrant.create_payload_index(
                    collection_name=KB_COLLECTION,
                    field_name="doc_id",
                    field_schema=PayloadSchemaType.KEYWORD,
                )
            except Exception as exc:
                if "already exists" not in str(exc) and "409" not in str(exc):
                    raise

    def _upsert_chunks(self, workspace_id: str, doc_id: str, chunks: list[dict]) -> None:
        from qdrant_client.models import PointStruct
        if not chunks:
            return

        texts = [c["text"] for c in chunks]
        embedder = self._get_embedding_client()
        embeddings = embedder.embed_documents(texts)

        qdrant = self._get_qdrant_client()
        self._ensure_collection(qdrant, len(embeddings[0]))

        points = [
            PointStruct(
                id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"{workspace_id}/{doc_id}/{c['chunk_index']}")),
                vector=embedding,
                payload={
                    "workspace_id": workspace_id,
                    "doc_id": doc_id,
                    "chunk_index": c["chunk_index"],
                    "text": c["text"],
                    "metadata": c.get("metadata", {}),
                },
            )
            for c, embedding in zip(chunks, embeddings)
        ]
        qdrant.upsert(collection_name=KB_COLLECTION, points=points)

    def _retrieve_semantic(
        self,
        workspace_id: str,
        query: str,
        chunks_from_db: list[dict],
        limit: int,
    ) -> list[dict]:
        from qdrant_client.models import Filter, FieldCondition, MatchValue

        embedder = self._get_embedding_client()
        if hasattr(embedder, "embed_query"):
            query_vector = embedder.embed_query(query)
        else:
            query_vector = embedder.embed([query])[0]

        qdrant = self._get_qdrant_client()

        # Check collection exists
        existing = {col.name for col in qdrant.get_collections().collections}
        if KB_COLLECTION not in existing:
            return self._retrieve_local(query, chunks_from_db, limit)

        results = qdrant.query_points(
            collection_name=KB_COLLECTION,
            query=query_vector,
            query_filter=Filter(
                must=[FieldCondition(key="workspace_id", match=MatchValue(value=workspace_id))]
            ),
            limit=limit,
            score_threshold=0.35,
        ).points

        # Map qdrant results back to full chunk records
        idx_map = {(c.get("metadata", {}).get("doc_id"), c.get("chunk_index")): c for c in chunks_from_db}

        found: list[dict] = []
        for hit in results:
            payload = hit.payload or {}
            chunk = {
                "text": payload.get("text", ""),
                "chunk_index": payload.get("chunk_index"),
                "metadata": payload.get("metadata", {}),
                "score": hit.score,
            }
            found.append(chunk)

        return found

    # ------------------------------------------------------------------
    # Local keyword fallback
    # ------------------------------------------------------------------

    def _retrieve_local(self, query: str, chunks: list[dict], limit: int) -> list[dict]:
        terms = {t for t in re.findall(r"[a-zA-Z0-9]+", query.lower()) if t not in STOPWORDS}
        if not terms:
            return chunks[:limit]

        scored: list[tuple[int, dict]] = []
        for chunk in chunks:
            haystack = chunk.get("text", "").lower()
            score = sum(1 for term in terms if term in haystack)
            if score:
                scored.append((score, chunk))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [c for _, c in scored[:limit]]
