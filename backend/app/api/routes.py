import shutil
import uuid
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models import KnowledgeDocument, KnowledgeChunk, KB_MAX_DOCUMENTS_PER_WORKSPACE, KB_MAX_FILE_SIZE_BYTES, KB_MAX_TOTAL_BYTES_PER_WORKSPACE
from app.repositories import WorkspaceRepository
from app.schemas import KnowledgeDocumentOut, WorkspaceOut
from app.services.agent import DocumentAgent
from app.services.run_store import run_store
from app.services.serializers import serialize_workspace
from app.services.uploads import UploadService

router = APIRouter()


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Workspaces CRUD
# ---------------------------------------------------------------------------

@router.get("/workspaces", response_model=list[WorkspaceOut])
def list_workspaces(db: Session = Depends(get_db)) -> list[WorkspaceOut]:
    repo = WorkspaceRepository(db)
    return [serialize_workspace(workspace, repo) for workspace in repo.list()]


@router.post("/workspaces", response_model=WorkspaceOut)
async def create_workspace(file: UploadFile = File(...), db: Session = Depends(get_db)) -> WorkspaceOut:
    if not file.filename:
        raise HTTPException(status_code=400, detail="Missing filename")
    suffix = Path(file.filename).suffix.lower()
    if suffix not in {".pptx", ".docx"}:
        raise HTTPException(status_code=400, detail="Only PPTX and DOCX files are supported")
    workspace = await UploadService(db).create_workspace(file)
    return serialize_workspace(workspace, WorkspaceRepository(db))


@router.get("/workspaces/{workspace_id}", response_model=WorkspaceOut)
def get_workspace(workspace_id: str, db: Session = Depends(get_db)) -> WorkspaceOut:
    repo = WorkspaceRepository(db)
    workspace = repo.get(workspace_id)
    if not workspace:
        raise HTTPException(status_code=404, detail="Workspace not found")
    return serialize_workspace(workspace, repo)


@router.post("/workspaces/{workspace_id}/rollback/{version_number}", response_model=WorkspaceOut)
def rollback(workspace_id: str, version_number: int, db: Session = Depends(get_db)) -> WorkspaceOut:
    repo = WorkspaceRepository(db)
    workspace = repo.get(workspace_id)
    version = repo.version(workspace_id, version_number)
    if not workspace or not version:
        raise HTTPException(status_code=404, detail="Version not found")
    workspace.current_version = version_number
    db.commit()
    db.refresh(workspace)
    return serialize_workspace(workspace, repo)


@router.delete("/workspaces/{workspace_id}", status_code=204)
def delete_workspace(workspace_id: str, db: Session = Depends(get_db)) -> None:
    from app.core.config import settings
    from app.services.retrieval import RetrievalService
    from app.services.kb_retrieval import KBRetrievalService

    repo = WorkspaceRepository(db)
    workspace = repo.get(workspace_id)
    if not workspace:
        raise HTTPException(status_code=404, detail="Workspace not found")

    db.delete(workspace)
    db.commit()
    RetrievalService().delete_workspace(workspace_id)
    KBRetrievalService().delete_workspace_kb(workspace_id)

    workspace_dir = settings.storage_root / workspace_id
    if workspace_dir.exists():
        shutil.rmtree(workspace_dir)


# ---------------------------------------------------------------------------
# File serving
# ---------------------------------------------------------------------------

@router.get("/files/{workspace_id}/{filename}")
def get_file(workspace_id: str, filename: str) -> FileResponse:
    from app.core.config import settings

    path = settings.storage_root / workspace_id / filename
    if not path.exists():
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(path)


@router.get("/source-files/{workspace_id}/{filename}")
def get_source_file(workspace_id: str, filename: str) -> FileResponse:
    """Serve source .docx/.pptx files so OnlyOffice can fetch them during conversion."""
    from app.core.config import settings

    path = (settings.storage_root / workspace_id / filename).resolve()
    storage = settings.storage_root.resolve()

    if not str(path).startswith(str(storage)):
        raise HTTPException(status_code=403, detail="Access denied")
    if not path.exists():
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(path)


# ---------------------------------------------------------------------------
# Chat — fire-and-forget (202 Accepted)
# ---------------------------------------------------------------------------

async def _run_agent_background(
    workspace_id: str,
    request: str,
    run_id: str,
    attached_image_path: str | None,
    db: Session,
    provider: str | None = None,
    model: str | None = None,
) -> None:
    """Background coroutine: runs the full pipeline and writes results to run_store."""
    try:
        await DocumentAgent(db, provider=provider, model=model).run(
            workspace_id,
            request,
            run_id=run_id,
            attached_image_path=attached_image_path,
        )
    except Exception as exc:
        run_store.fail(run_id, str(exc))
    finally:
        db.close()


@router.post("/chat", status_code=202)
async def chat(
    background_tasks: BackgroundTasks,
    workspace_id: str = Form(...),
    content: str = Form(...),
    image: UploadFile | None = File(None),
    provider: str | None = Form(None),
    model: str | None = Form(None),
    db: Session = Depends(get_db),
) -> dict:
    from app.core.config import settings

    repo = WorkspaceRepository(db)
    if not repo.get(workspace_id):
        raise HTTPException(status_code=404, detail="Workspace not found")

    # Save attached image if present
    attached_image_path: str | None = None
    if image and image.filename:
        suffix = Path(image.filename).suffix.lower()
        if suffix not in {".png", ".jpg", ".jpeg", ".webp", ".svg", ".gif", ".bmp"}:
            raise HTTPException(
                status_code=400,
                detail="Unsupported image type. Please attach PNG, JPG, WEBP, or SVG.",
            )
        uploads_dir = settings.storage_root / workspace_id / "uploads"
        uploads_dir.mkdir(parents=True, exist_ok=True)
        import time
        safe_name = f"{int(time.time() * 1000)}{suffix}"
        dest = uploads_dir / safe_name
        with dest.open("wb") as f:
            shutil.copyfileobj(image.file, f)
        attached_image_path = str(dest)

    # Create a polling entry immediately (so /polling returns "running" right away)
    run_id = str(uuid.uuid4())
    run_store.create(run_id, workspace_id)

    # Spawn the pipeline as a background task
    background_tasks.add_task(
        _run_agent_background,
        workspace_id,
        content,
        run_id,
        attached_image_path,
        db,
        provider,
        model,
    )

    return {"run_id": run_id}


# ---------------------------------------------------------------------------
# Polling — returns current run status + accumulated events
# ---------------------------------------------------------------------------

@router.get("/polling/{run_id}")
def poll_run(run_id: str) -> dict:
    """Return the current state of an agent run.

    Response shape:
      {
        "status": "running" | "completed" | "error" | "not_found",
        "events": [ {type, ...}, ... ],   # all events accumulated so far
        "workspace": <WorkspaceOut> | null,
        "error": <str> | null
      }
    """
    return run_store.snapshot(run_id)


# ---------------------------------------------------------------------------
# Knowledge Base
# ---------------------------------------------------------------------------

_KB_ALLOWED_TYPES = {".pdf", ".docx", ".txt", ".md"}


@router.get("/workspaces/{workspace_id}/knowledge", response_model=list[KnowledgeDocumentOut])
def list_knowledge_documents(workspace_id: str, db: Session = Depends(get_db)) -> list[KnowledgeDocumentOut]:
    """List all knowledge base documents for a workspace."""
    repo = WorkspaceRepository(db)
    if not repo.get(workspace_id):
        raise HTTPException(status_code=404, detail="Workspace not found")
    docs = repo.list_knowledge_documents(workspace_id)
    return [
        KnowledgeDocumentOut(
            id=d.id,
            filename=d.filename,
            file_type=d.file_type,
            file_size_bytes=d.file_size_bytes,
            chunk_count=d.chunk_count,
            status=d.status,
            error_message=d.error_message,
            created_at=d.created_at,
        )
        for d in docs
    ]


def _process_kb_document_bg(
    doc_id: str,
    file_path: str,
    file_type: str,
    workspace_id: str,
    db_session_factory,
) -> None:
    """Background task: parse, chunk, and index a KB document."""
    from app.db.session import SessionLocal
    from app.services.kb_processor import KBProcessor
    from app.services.kb_retrieval import KBRetrievalService

    db = SessionLocal()
    try:
        doc = db.get(KnowledgeDocument, doc_id)
        if not doc:
            return

        processor = KBProcessor()
        chunks = processor.process_document(Path(file_path), file_type)

        # Store chunks in DB
        for chunk in chunks:
            db_chunk = KnowledgeChunk(
                document_id=doc_id,
                workspace_id=workspace_id,
                chunk_index=chunk["chunk_index"],
                text=chunk["text"],
                chunk_metadata=chunk.get("metadata", {}),
            )
            db.add(db_chunk)

        doc.chunk_count = len(chunks)
        doc.status = "indexed"
        db.commit()

        # Index in Qdrant
        KBRetrievalService().index_document(workspace_id, doc_id, chunks)

    except Exception as exc:
        import logging
        logging.getLogger(__name__).error("KB processing failed for %s: %s", doc_id, exc)
        try:
            doc = db.get(KnowledgeDocument, doc_id)
            if doc:
                doc.status = "failed"
                doc.error_message = str(exc)[:500]
                db.commit()
        except Exception:
            pass
    finally:
        db.close()


@router.post("/workspaces/{workspace_id}/knowledge", response_model=KnowledgeDocumentOut, status_code=202)
async def upload_knowledge_document(
    workspace_id: str,
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
) -> KnowledgeDocumentOut:
    """Upload a document to the workspace knowledge base.

    Processing (parsing, chunking, embedding) happens in the background.
    Returns immediately with the document record and status='processing'.

    Accepted file types: .pdf, .docx, .txt, .md
    Limits: max 50 documents per workspace, max 50 MB per file, max 100 MB total.
    """
    from app.core.config import settings

    repo = WorkspaceRepository(db)
    if not repo.get(workspace_id):
        raise HTTPException(status_code=404, detail="Workspace not found")

    if not file.filename:
        raise HTTPException(status_code=400, detail="Missing filename")

    suffix = Path(file.filename).suffix.lower()
    if suffix not in _KB_ALLOWED_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type '{suffix}'. Allowed: {', '.join(_KB_ALLOWED_TYPES)}",
        )

    # Enforce limits
    doc_count = repo.count_knowledge_documents(workspace_id)
    if doc_count >= KB_MAX_DOCUMENTS_PER_WORKSPACE:
        raise HTTPException(
            status_code=400,
            detail=f"Knowledge base limit reached ({KB_MAX_DOCUMENTS_PER_WORKSPACE} documents max).",
        )

    # Read file bytes to check size
    content = await file.read()
    file_size = len(content)

    if file_size > KB_MAX_FILE_SIZE_BYTES:
        raise HTTPException(
            status_code=400,
            detail=f"File too large ({file_size // 1024 // 1024} MB). Max 50 MB per file.",
        )

    total_bytes = repo.total_knowledge_size_bytes(workspace_id)
    if total_bytes + file_size > KB_MAX_TOTAL_BYTES_PER_WORKSPACE:
        raise HTTPException(
            status_code=400,
            detail="Workspace knowledge base total size limit (100 MB) exceeded.",
        )

    # Save file to disk
    kb_dir = settings.storage_root / workspace_id / "knowledge"
    kb_dir.mkdir(parents=True, exist_ok=True)
    safe_name = f"{uuid.uuid4()}{suffix}"
    file_path = kb_dir / safe_name
    file_path.write_bytes(content)

    # Create DB record
    doc = KnowledgeDocument(
        workspace_id=workspace_id,
        filename=file.filename,
        file_type=suffix.lstrip("."),
        file_path=str(file_path),
        file_size_bytes=file_size,
        status="processing",
    )
    db.add(doc)
    db.commit()
    db.refresh(doc)

    # Queue background processing
    background_tasks.add_task(
        _process_kb_document_bg,
        doc.id,
        str(file_path),
        doc.file_type,
        workspace_id,
        None,  # db_session_factory placeholder (background task creates its own session)
    )

    return KnowledgeDocumentOut(
        id=doc.id,
        filename=doc.filename,
        file_type=doc.file_type,
        file_size_bytes=doc.file_size_bytes,
        chunk_count=doc.chunk_count,
        status=doc.status,
        error_message=doc.error_message,
        created_at=doc.created_at,
    )


@router.get("/workspaces/{workspace_id}/knowledge/{document_id}/status")
def knowledge_document_status(
    workspace_id: str,
    document_id: str,
    db: Session = Depends(get_db),
) -> dict:
    """Check processing status of a KB document."""
    repo = WorkspaceRepository(db)
    doc = repo.get_knowledge_document(document_id)
    if not doc or doc.workspace_id != workspace_id:
        raise HTTPException(status_code=404, detail="Document not found")
    return {
        "id": doc.id,
        "status": doc.status,
        "chunk_count": doc.chunk_count,
        "error_message": doc.error_message,
    }


@router.delete("/workspaces/{workspace_id}/knowledge/{document_id}", status_code=204)
def delete_knowledge_document(
    workspace_id: str,
    document_id: str,
    db: Session = Depends(get_db),
) -> None:
    """Remove a KB document and its chunks from both DB and Qdrant."""
    from app.services.kb_retrieval import KBRetrievalService

    repo = WorkspaceRepository(db)
    doc = repo.get_knowledge_document(document_id)
    if not doc or doc.workspace_id != workspace_id:
        raise HTTPException(status_code=404, detail="Document not found")

    # Remove Qdrant vectors first
    KBRetrievalService().delete_document(workspace_id, document_id)

    # Remove file from disk
    try:
        file_path = Path(doc.file_path)
        if file_path.exists():
            file_path.unlink()
    except Exception:
        pass

    # Remove from DB (chunks cascade)
    db.delete(doc)
    db.commit()
