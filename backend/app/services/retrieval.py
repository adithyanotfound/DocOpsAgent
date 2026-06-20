import re
import uuid

from app.core.config import settings

STOPWORDS = {
    "a", "all", "and", "are", "for", "find", "in",
    "make", "of", "on", "remove", "rewrite", "shorter",
    "the", "them", "to",
}

COLLECTION_NAME = "document_blocks"


class RetrievalService:
    """Retrieves relevant document blocks for a given query.

    Uses Qdrant vector search when both ``qdrant_url`` and ``openai_api_key``
    are configured.  Falls back to the local lexical scorer otherwise.
    """

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def retrieve(self, query: str, structure: dict, limit: int = 4) -> list[dict]:
        if settings.qdrant_url and settings.openai_api_key:
            try:
                return self._retrieve_semantic(query, structure, limit)
            except Exception:
                pass
        return self._retrieve_local(query, structure, limit)

    def index_workspace(self, workspace_id: str, structure: dict) -> None:
        """Upsert all document blocks for a workspace into Qdrant."""
        if not (settings.qdrant_url and settings.openai_api_key):
            return
        try:
            self._upsert_blocks(workspace_id, structure)
        except Exception:
            pass

    def delete_workspace(self, workspace_id: str) -> None:
        """Delete all document blocks for a workspace from Qdrant."""
        if not (settings.qdrant_url and settings.openai_api_key):
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
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Qdrant / embedding path
    # ------------------------------------------------------------------

    def _get_qdrant_client(self):
        from qdrant_client import QdrantClient

        return QdrantClient(
            url=settings.qdrant_url,
            api_key=settings.qdrant_api_key or None,
        )

    def _get_openai_client(self):
        from openai import OpenAI

        return OpenAI(
            api_key=settings.openai_api_key,
            base_url=settings.openai_base_url or None,
        )

    def _embed(self, texts: list[str]) -> list[list[float]]:
        client = self._get_openai_client()
        response = client.embeddings.create(
            model=settings.embedding_model,
            input=texts,
        )
        return [item.embedding for item in response.data]

    def _ensure_collection(self, qdrant, vector_size: int) -> None:
        from qdrant_client.models import Distance, VectorParams

        existing = {col.name for col in qdrant.get_collections().collections}
        if COLLECTION_NAME not in existing:
            qdrant.create_collection(
                collection_name=COLLECTION_NAME,
                vectors_config=VectorParams(size=vector_size, distance=Distance.COSINE),
            )

    def _upsert_blocks(self, workspace_id: str, structure: dict) -> None:
        from qdrant_client.models import PointStruct

        blocks = structure.get("blocks", [])
        if not blocks:
            return

        texts = [block["text"] for block in blocks]
        embeddings = self._embed(texts)

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

        query_vector = self._embed([query])[0]
        qdrant = self._get_qdrant_client()

        # We don't filter by workspace here since structure already scopes blocks.
        # We pass block texts as the lookup and match on element_id.
        results = qdrant.search(
            collection_name=COLLECTION_NAME,
            query_vector=query_vector,
            limit=limit,
            score_threshold=0.45,
        )

        # Return blocks from the current structure that match retrieved element_ids.
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
