import asyncio
from qdrant_client import QdrantClient
from app.core.config import settings

def clear_collection():
    if not settings.qdrant_url:
        print("No qdrant URL set.")
        return

    print(f"Connecting to Qdrant at {settings.qdrant_url}...")
    client = QdrantClient(
        url=settings.qdrant_url,
        api_key=settings.qdrant_api_key or None,
    )
    
    collection_name = "document_blocks"
    try:
        client.delete_collection(collection_name)
        print(f"Collection '{collection_name}' deleted successfully.")
    except Exception as e:
        print(f"Failed to delete collection: {e}")

if __name__ == "__main__":
    clear_collection()
