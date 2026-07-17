import re
import uuid
import logging
import hashlib

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
        elif settings.llm_provider == "openrouter":
            from app.services.embedding_client import OpenRouterEmbeddingClient
            return OpenRouterEmbeddingClient()
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
                id=str(uuid.uuid4()),
                vector=embedding,
                payload={
                    "workspace_id": workspace_id,
                    "element_id": block["element_id"],
                    "text": block["text"],
                    "text_hash": hashlib.md5(block["text"].encode("utf-8")).hexdigest(),
                    "metadata": block.get("metadata", {}),
                    "type": block.get("type", "text"),
                },
            )
            for block, embedding in zip(blocks, embeddings)
        ]
        qdrant.upsert(collection_name=COLLECTION_NAME, points=points)

    def sync_workspace(self, workspace_id: str, structure: dict) -> None:
        """Delta-sync the workspace blocks to Qdrant without unnecessary re-embeddings."""
        if not (settings.qdrant_url and (settings.gemini_api_key or settings.openai_api_key)):
            return
            
        blocks = structure.get("blocks", [])
        if not blocks:
            # If document is empty, just clear the workspace from Qdrant
            self.delete_workspace(workspace_id)
            return
            
        try:
            from qdrant_client.models import Filter, FieldCondition, MatchValue, PointStruct, PointIdsList
            qdrant = self._get_qdrant_client()
            
            # Step 1: Scroll existing metadata with pagination
            old_points = []
            next_page_offset = None
            scroll_filter = Filter(must=[FieldCondition(key="workspace_id", match=MatchValue(value=workspace_id))])
            
            while True:
                pts, next_page_offset = qdrant.scroll(
                    collection_name=COLLECTION_NAME,
                    scroll_filter=scroll_filter,
                    with_payload=True,
                    with_vectors=False,
                    limit=1000,
                    offset=next_page_offset
                )
                old_points.extend(pts)
                if next_page_offset is None:
                    break
            
            from collections import defaultdict
            hash_to_available_qids = defaultdict(list)
            for pt in old_points:
                thash = pt.payload.get("text_hash")
                if thash:
                    hash_to_available_qids[thash].append((pt.id, pt.payload.get("element_id")))
                    
            # Step 2: Categorize live blocks
            points_to_upsert = []
            texts_to_embed = []
            blocks_waiting_for_embed = []
            
            payload_updates = [] # list of (qid, new_payload)
            live_qids = set()
            
            for block in blocks:
                text = block.get("text", "")
                thash = hashlib.md5(text.encode("utf-8")).hexdigest()
                current_element_id = block["element_id"]
                
                if hash_to_available_qids[thash]:
                    # Prefer popping a QID that perfectly matches the element_id to avoid unnecessary updates
                    match_idx = -1
                    for idx, (qid, old_element_id) in enumerate(hash_to_available_qids[thash]):
                        if old_element_id == current_element_id:
                            match_idx = idx
                            break
                            
                    if match_idx != -1:
                        qid, _ = hash_to_available_qids[thash].pop(match_idx)
                        live_qids.add(qid)
                        continue # Unchanged
                    else:
                        qid, _ = hash_to_available_qids[thash].pop(0)
                        live_qids.add(qid)
                        # Shifted
                        payload_updates.append((qid, {
                            "workspace_id": workspace_id,
                            "element_id": current_element_id,
                            "text": text,
                            "text_hash": thash,
                            "metadata": block.get("metadata", {}),
                            "type": block.get("type", "text")
                        }))
                        continue
                        
                # New content
                qid = str(uuid.uuid4())
                live_qids.add(qid)
                texts_to_embed.append(text)
                blocks_waiting_for_embed.append((qid, block, thash))
                        
            # Step 3: Update payloads for Shifted Vectors
            for qid, payload in payload_updates:
                qdrant.set_payload(
                    collection_name=COLLECTION_NAME,
                    payload=payload,
                    points=[qid]
                )
                        
            # Step 4: Embed New Content
            if texts_to_embed:
                embedder = self._get_embedding_client()
                
                # Deduplicate embeddings based on text_hash
                unique_texts = {}
                for text in texts_to_embed:
                    h = hashlib.md5(text.encode("utf-8")).hexdigest()
                    if h not in unique_texts:
                        unique_texts[h] = text
                        
                unique_hashes = list(unique_texts.keys())
                unique_strings = list(unique_texts.values())
                
                if hasattr(embedder, "embed_documents"):
                    unique_embeddings = embedder.embed_documents(unique_strings)
                else:
                    unique_embeddings = embedder.embed(unique_strings)
                    
                hash_to_embedding = dict(zip(unique_hashes, unique_embeddings))
                
                for qid, block, thash in blocks_waiting_for_embed:
                    if thash in hash_to_embedding:
                        points_to_upsert.append(PointStruct(
                            id=qid,
                            vector=hash_to_embedding[thash],
                            payload={
                                "workspace_id": workspace_id,
                                "element_id": block["element_id"],
                                "text": block["text"],
                                "text_hash": thash,
                                "metadata": block.get("metadata", {}),
                                "type": block.get("type", "text")
                            }
                        ))

            # Step 5: Upsert First (for New Content)
            if points_to_upsert:
                self._ensure_collection(qdrant, len(points_to_upsert[0].vector))
                qdrant.upsert(collection_name=COLLECTION_NAME, points=points_to_upsert)
                
            # Step 6: Delete Stale Points
            stale_qids = [pt.id for pt in old_points if pt.id not in live_qids]
            if stale_qids:
                qdrant.delete(
                    collection_name=COLLECTION_NAME,
                    points_selector=PointIdsList(points=stale_qids)
                )
                
        except Exception as exc:
            log.warning("Workspace sync failed: %s", exc)

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
