import asyncio
import logging
from app.services.retrieval import RetrievalService
from app.core.config import settings
from unittest.mock import patch
import hashlib
from qdrant_client.models import Filter, FieldCondition, MatchValue

logging.basicConfig(level=logging.DEBUG)

def main():
    rs = RetrievalService()
    workspace_id = "test-sync-workspace-v3"
    
    # 1. Initial structure with 4 paragraphs (one duplicate)
    structure_v1 = {
        "blocks": [
            {"element_id": "paragraph_0", "text": "This is paragraph 0", "type": "paragraph"},
            {"element_id": "paragraph_1", "text": "This is paragraph 1", "type": "paragraph"},
            {"element_id": "paragraph_2", "text": "This is paragraph 2", "type": "paragraph"},
            {"element_id": "paragraph_3", "text": "Duplicate Text", "type": "paragraph"},
            {"element_id": "paragraph_4", "text": "Duplicate Text", "type": "paragraph"},
        ]
    }
    
    # Ensure clean slate
    rs.delete_workspace(workspace_id)
    
    print("\n--- Initial Sync ---")
    rs.sync_workspace(workspace_id, structure_v1)
    
    # 2. Structure v2: 
    # - Insert new at top (shifts everything)
    # - Edit paragraph 2 (old paragraph 1) -> EDITED
    # - Delete paragraph 4 (old paragraph 3) -> DELETED
    # - paragraph 1 (old 0) -> SHIFTED
    # - paragraph 5 (old 4) -> SHIFTED (Duplicate Text)
    
    structure_v2 = {
        "blocks": [
            {"element_id": "paragraph_0", "text": "Brand new inserted paragraph!", "type": "paragraph"}, # NEW
            {"element_id": "paragraph_1", "text": "This is paragraph 0", "type": "paragraph"}, # SHIFTED
            {"element_id": "paragraph_2", "text": "This is paragraph 1 with an edit!", "type": "paragraph"}, # EDITED/NEW
            {"element_id": "paragraph_3", "text": "This is paragraph 2", "type": "paragraph"}, # SHIFTED
            {"element_id": "paragraph_4", "text": "Duplicate Text", "type": "paragraph"}, # SHIFTED (Was 4, now 4? Wait, if we deleted 3 and inserted 0, index 4 stays index 4. So UNCHANGED!)
        ]
    }
    
    print("\n--- Second Sync (Shift + Edit + Insert) ---")
    qdrant = rs._get_qdrant_client()
    
    original_set_payload = qdrant.set_payload
    original_upsert = qdrant.upsert
    original_delete = qdrant.delete
    
    stats = {"set_payload": 0, "upsert": 0, "delete": 0}
    
    def mock_set_payload(*args, **kwargs):
        stats["set_payload"] += len(kwargs.get("points", []))
        return original_set_payload(*args, **kwargs)
        
    def mock_upsert(*args, **kwargs):
        stats["upsert"] += len(kwargs.get("points", []))
        return original_upsert(*args, **kwargs)
        
    def mock_delete(*args, **kwargs):
        stats["delete"] += len(kwargs.get("points_selector").points)
        return original_delete(*args, **kwargs)

    def mock_get_qdrant_client():
        return qdrant

    with patch.object(qdrant, 'set_payload', side_effect=mock_set_payload), \
         patch.object(qdrant, 'upsert', side_effect=mock_upsert), \
         patch.object(qdrant, 'delete', side_effect=mock_delete), \
         patch.object(rs, '_get_qdrant_client', side_effect=mock_get_qdrant_client):
        
        rs.sync_workspace(workspace_id, structure_v2)
        
    print("\n--- Verification ---")
    from app.services.retrieval import COLLECTION_NAME
    res, _ = qdrant.scroll(
        collection_name=COLLECTION_NAME,
        scroll_filter=Filter(must=[FieldCondition(key="workspace_id", match=MatchValue(value=workspace_id))])
    )
    
    print(f"Points in Qdrant after second sync: {len(res)}")
    for r in res:
        print(f" - [{r.id}] {r.payload['element_id']}: {r.payload['text']}")

    print(f"Stats: {stats}")
    
    # Assertions
    assert len(res) == 5, f"Expected 5 points, got {len(res)}"
    
    # "Brand new inserted paragraph!" and "This is paragraph 1 with an edit!" are new
    assert stats["upsert"] == 2, f"Expected 2 upserts (new content), got {stats['upsert']}"
    
    # paragraph 0 (now 1), paragraph 2 (now 3) shifted. 
    # paragraph 4 (Duplicate Text) was index 4, is STILL index 4, so UNCHANGED! 
    # Wait, the old doc had 0, 1, 2, 3, 4. 
    # The new doc has 0, 1, 2, 3, 4.
    # New doc 4 is "Duplicate Text". Old doc 4 was "Duplicate Text". 
    # Because of the popping logic, hash("Duplicate Text") has two QIDs. 
    # It will pop the one that matches element_id "paragraph_4" first! 
    # So paragraph_4 is Unchanged.
    # paragraph 3 ("Duplicate Text" in old) is no longer there. It will be deleted.
    # Therefore: 2 shifted (0->1, 2->3).
    
    assert stats["set_payload"] == 2, f"Expected 2 set_payloads (shifted), got {stats['set_payload']}"
    
    # Old paragraph 1 is deleted (replaced by edit). Old paragraph 3 is deleted.
    assert stats["delete"] == 2, f"Expected 2 deletes (stale), got {stats['delete']}"

    print("\nSUCCESS! All assertions passed.")

    print("\nCleaning up...")
    rs.delete_workspace(workspace_id)
    print("Done!")

if __name__ == "__main__":
    main()
