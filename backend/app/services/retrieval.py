import re
import uuid
import logging

from app.core.config import settings

log = logging.getLogger(__name__)

STOPWORDS = {
    "a", "all", "and", "are", "for", "find", "in",
    "make", "of", "on", "remove", "rewrite", "shorter",
    "the", "them", "to",
}

COLLECTION_NAME = "document_blocks"


class RetrievalService:
    """Retrieves relevant document blocks for a given query.

    Uses Qdrant vector search when configured, otherwise falls back to
    local lexical scoring. Supports both OpenAI embeddings and Gemini embeddings
    via the GeminiEmbeddingClient.
    """

    def __init__(self, embed_client=None) -> None:
        self._embed_client = embed_client

    def _get_embedding_client(self):
        if self._embed_client:
            return self._embed_client
        
        if settings.llm_provider == "gemini":
            from app.services.embedding_client import GeminiEmbeddingClient
            return GeminiEmbeddingClient()
        else:
            # Fallback to OpenAI embedding method
            from app.services.embedding_client import OpenAIEmbeddingClient
            return OpenAIEmbeddingClient()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def retrieve(self, query: str, structure: dict, limit: int = 4) -> list[dict]:
        if settings.qdrant_url and (settings.gemini_api_key or settings.openai_api_key):
            try:
                return self._retrieve_semantic(query, structure, limit)
            except Exception as exc:
                log.warning("Semantic retrieval failed: %s. Falling back to local.", exc)
                pass
        return self._retrieve_local(query, structure, limit)

    def index_workspace(self, workspace_id: str, structure: dict) -> None:
        """Upsert all document blocks for a workspace into Qdrant."""
        if not (settings.qdrant_url and (settings.gemini_api_key or settings.openai_api_key)):
            return
        try:
            self._upsert_blocks(workspace_id, structure)
        except Exception as exc:
            log.warning("Workspace indexing failed: %s", exc)
            pass

    def delete_workspace(self, workspace_id: str) -> None:
        """Delete all document blocks for a workspace from Qdrant."""
        if not (settings.qdrant_url and (settings.gemini_api_key or settings.openai_api_key)):
            return
        try:
            from qdrant_client.models import Filter, FieldCondition, MatchValue
            qdrant = self._get_qdrant_client()
            qdrant.delete(
                collection_name=COLLECTION_NAME,
                points_selector=Filter(
                    must=[
                        FieldCondition(
                            key="workspace_id",
                            match=MatchValue(value=workspace_id),
                        ),
                    ],
                ),
            )
        except Exception as exc:
            log.warning("Workspace deletion failed: %s", exc)
            pass

    # ------------------------------------------------------------------
    # Qdrant path
    # ------------------------------------------------------------------

    def _get_qdrant_client(self):
        from qdrant_client import QdrantClient

        return QdrantClient(
            url=settings.qdrant_url,
            api_key=settings.qdrant_api_key or None,
            timeout=60.0,
        )

    def _ensure_collection(self, qdrant, vector_size: int) -> None:
        from qdrant_client.models import Distance, VectorParams, PayloadSchemaType

        existing = {col.name for col in qdrant.get_collections().collections}
        if COLLECTION_NAME not in existing:
            qdrant.create_collection(
                collection_name=COLLECTION_NAME,
                vectors_config=VectorParams(size=vector_size, distance=Distance.COSINE),
            )
            qdrant.create_payload_index(
                collection_name=COLLECTION_NAME,
                field_name="workspace_id",
                field_schema=PayloadSchemaType.KEYWORD,
            )

    def _upsert_blocks(self, workspace_id: str, structure: dict) -> None:
        from qdrant_client.models import PointStruct

        blocks = structure.get("blocks", [])
        if not blocks:
            return

        texts = [block["text"] for block in blocks]
        
        embedder = self._get_embedding_client()
        # Use appropriate method name depending on type
        if hasattr(embedder, "embed_documents"):
            embeddings = embedder.embed_documents(texts)
        else:
            embeddings = embedder.embed(texts)

        qdrant = self._get_qdrant_client()
        self._ensure_collection(qdrant, len(embeddings[0]))

        points = [
            PointStruct(
                id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"{workspace_id}/{block['element_id']}")),
                vector=embedding,
                payload={
                    "workspace_id": workspace_id,
                    "element_id": block["element_id"],
                    "text": block["text"],
                    "metadata": block.get("metadata", {}),
                    "type": block.get("type", "text"),
                },
            )
            for block, embedding in zip(blocks, embeddings)
        ]
        qdrant.upsert(collection_name=COLLECTION_NAME, points=points)

    def _retrieve_semantic(self, query: str, structure: dict, limit: int) -> list[dict]:
        from qdrant_client.models import Filter, FieldCondition, MatchValue

        # Build a mapping of element_id -> block for re-ranking results
        blocks_by_id = {block["element_id"]: block for block in structure.get("blocks", [])}
        if not blocks_by_id:
            return []

        embedder = self._get_embedding_client()
        if hasattr(embedder, "embed_query"):
            query_vector = embedder.embed_query(query)
        else:
            query_vector = embedder.embed([query])[0]

        qdrant = self._get_qdrant_client()

        results = qdrant.query_points(
            collection_name=COLLECTION_NAME,
            query=query_vector,
            limit=limit,
            score_threshold=0.45,
        ).points

        found: list[dict] = []
        for hit in results:
            eid = hit.payload.get("element_id") if hit.payload else None
            if eid and eid in blocks_by_id:
                found.append(blocks_by_id[eid])
        return found

    # ------------------------------------------------------------------
    # Local lexical fallback
    # ------------------------------------------------------------------

    def _retrieve_local(self, query: str, structure: dict, limit: int) -> list[dict]:
        terms = {term for term in re.findall(r"[a-zA-Z0-9]+", query.lower()) if term not in STOPWORDS}
        scored: list[tuple[int, dict]] = []
        for block in structure.get("blocks", []):
            haystack = block["text"].lower()
            score = sum(1 for term in terms if term in haystack)
            if score:
                scored.append((score, block))
        scored.sort(key=lambda item: item[0], reverse=True)
        return [block for _, block in scored[:limit]]
