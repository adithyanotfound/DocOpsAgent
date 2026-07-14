import asyncio
import json
import logging
from typing import Any
import shutil
from pathlib import Path

from sqlalchemy.orm import Session

from app.db.session import SessionLocal
from app.models import Workspace, DocumentVersion, DocumentStructure, Message, AgentRun
from app.services.agent import DocumentAgent
from app.services.storage import StorageService
from app.services.llm_client import LLMClient
from app.services.document_processor import DocumentProcessor
from app.services.outline_builder import OutlineBuilder
from app.services.preview import PreviewService
async def mock_convert_to_pdf(self, source: Path, dest: Path) -> Path:
    import asyncio
    await asyncio.to_thread(self._placeholder, source, dest)
    return dest
PreviewService.convert_to_pdf = mock_convert_to_pdf

from app.services.retrieval import RetrievalService
from app.services.run_store import run_store

from tests.eval.prompts import PROMPT_LIST

# 1. Setup File Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("eval_results.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

async def run_eval() -> None:
    db: Session = SessionLocal()
    storage = StorageService()
    llm = LLMClient()
    processor = DocumentProcessor()
    retrieval_service = RetrievalService()

    passed = 0
    failed = 0
    looping_warnings = 0

    logger.info("="*60)
    logger.info("Starting Evaluation Harness")
    logger.info("="*60)

    # 2. Master Workspace Setup (Pre-loop)
    workspace = Workspace(document_type="docx", original_filename="Sample_Word_Template.docx", current_version=1)
    db.add(workspace)
    db.commit()
    db.refresh(workspace)
    
    sample_path = Path("../samples/Sample_Word_Template.docx")
    if not sample_path.exists():
        logger.error("Sample template not found at ../samples/Sample_Word_Template.docx")
        return

    # Extract initial structure once
    target_path = storage.version_document_path(workspace.id, 1, "docx")
    shutil.copy(sample_path, target_path)

    extracted_before = processor.extract(target_path, "docx")
    structure_json = OutlineBuilder.build(extracted_before, "docx")
    
    structure_row = DocumentStructure(
        workspace_id=workspace.id,
        version_number=1,
        structure_json=extracted_before
    )
    db.add(structure_row)
    db.commit()

    # Index embeddings ONCE
    logger.info("Indexing embeddings for the master workspace...")
    try:
        retrieval_service.index_workspace(workspace.id, structure_json)
        logger.info("Embeddings indexed successfully.")
    except Exception as e:
        logger.warning(f"Failed to index embeddings (is Qdrant configured?): {e}")

    filtered_prompts = [p for p in PROMPT_LIST if p["id"] == "table_edit_Q3"]
    total_prompts = len(filtered_prompts)

    for idx, item in enumerate(filtered_prompts, start=1):
        prompt_id = item["id"]
        category = item["category"]
        prompt = item["prompt"]
        eval_type = item["type"]
        assertion = item["assertion"]

        logger.info(f"\n[{idx}/{total_prompts}] Evaluating [{category}] {prompt_id}: '{prompt}'")
        
        # 3. Workspace Reset (Inside Loop)
        # Ensure current_version is 1
        workspace.current_version = 1
        db.commit()
        
        # Delete DB rows for versions > 1
        db.query(DocumentVersion).filter(DocumentVersion.workspace_id == workspace.id, DocumentVersion.version_number > 1).delete()
        db.query(DocumentStructure).filter(DocumentStructure.workspace_id == workspace.id, DocumentStructure.version_number > 1).delete()
        
        # Clear chat history and agent runs
        db.query(Message).filter(Message.workspace_id == workspace.id).delete()
        db.query(AgentRun).filter(AgentRun.workspace_id == workspace.id).delete()
        db.commit()
        
        # Re-copy the pristine sample document for v1 just in case previous graph operations mutated it in place
        shutil.copy(sample_path, target_path)

        # Make sure v1 DocumentVersion exists in DB
        v1_doc = db.query(DocumentVersion).filter(DocumentVersion.workspace_id == workspace.id, DocumentVersion.version_number == 1).first()
        if not v1_doc:
            v1_doc = DocumentVersion(
                workspace_id=workspace.id,
                version_number=1,
                document_path=str(target_path),
                pdf_path=str(storage.version_pdf_path(workspace.id, 1))
            )
            db.add(v1_doc)
            db.commit()

        # 4. Run agent
        agent = DocumentAgent(db)
        run_id = f"test_{workspace.id}_{prompt_id}"
        
        # Execute agent
        logger.info(f"  Running Agent...")
        try:
            run = await agent.run(workspace.id, prompt, run_id=run_id)
        except Exception as e:
            logger.error(f"  [x] FAIL: Agent run threw exception: {e}")
            failed += 1
            continue
        
        # Check loops
        entry = run_store.get(run_id)
        max_iter = 0
        if entry:
            max_iter = max([e.get("iteration", 0) for e in entry.events if "iteration" in e], default=0)
            
        if max_iter > 1: # Iteration starts at 0, goes to 1 on retry. >1 means it looped to retry again.
            logger.warning(f"  [!] Looping Warning: Agent took {max_iter + 1} iterations to complete.")
            looping_warnings += 1

        # 5. Evaluate Output
        db.refresh(workspace)
        latest_doc = db.query(DocumentVersion).filter(
            DocumentVersion.workspace_id == workspace.id,
            DocumentVersion.version_number == workspace.current_version
        ).first()
        
        if not latest_doc or workspace.current_version == 1:
            logger.error("  [x] FAIL: No new version created.")
            failed += 1
            continue
            
        new_doc_path = Path(latest_doc.document_path)
        extracted_after = processor.extract(new_doc_path, "docx")
        after_structure = OutlineBuilder.build(extracted_after, "docx")

        if eval_type == "subjective":
            sys_prompt = "You are an objective document evaluation judge."
            user_prompt = f"""
Original Prompt: {prompt}

Document Structure BEFORE:
{json.dumps(extracted_before, indent=2)}

Document Structure AFTER:
{json.dumps(after_structure, indent=2)}

Evaluation Criteria: {assertion}
Examine the Document Structure AFTER and determine if the criteria is satisfied.
Respond with exactly one word: PASS or FAIL.
"""
            from app.services.llm_client import LLMRequest
            response = llm.complete(LLMRequest(
                system_prompt=sys_prompt,
                user_prompt=user_prompt,
                temperature=0,
                max_tokens=10,
            ))
            
            result = response.text.strip().upper()
            if "PASS" in result:
                logger.info("  [✓] PASS")
                passed += 1
            else:
                logger.error("  [x] FAIL")
                failed += 1
                logger.error(f"      LLM Feedback: {result}")
        else:
            logger.info("  [-] SKIP: Deterministic eval not implemented yet.")

    # Optional: cleanup master workspace to save space
    db.query(Workspace).filter(Workspace.id == workspace.id).delete()
    db.commit()

    logger.info("="*60)
    logger.info("Evaluation Complete")
    logger.info(f"Passed: {passed}")
    logger.info(f"Failed: {failed}")
    logger.info(f"Looping Warnings: {looping_warnings}")
    logger.info("="*60)

if __name__ == "__main__":
    asyncio.run(run_eval())
