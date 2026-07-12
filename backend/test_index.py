import asyncio
from app.services.retrieval import RetrievalService
from app.core.config import settings

def test_index():
    print("API Key:", settings.gemini_api_key)
    retrieval = RetrievalService()
    structure = {
        "blocks": [
            {
                "element_id": "block1",
                "text": "This is a test document block for indexing.",
                "metadata": {"role": "paragraph"}
            }
        ]
    }
    workspace_id = "test-workspace-123"
    
    print("Indexing test workspace...")
    retrieval.index_workspace(workspace_id, structure)
    print("Indexed successfully!")
    
    print("Deleting test workspace...")
    retrieval.delete_workspace(workspace_id)
    print("Deleted successfully!")

if __name__ == "__main__":
    test_index()
