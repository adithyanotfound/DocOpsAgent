import shutil
import uuid
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.repositories import WorkspaceRepository
from app.schemas import WorkspaceOut
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

    repo = WorkspaceRepository(db)
    workspace = repo.get(workspace_id)
    if not workspace:
        raise HTTPException(status_code=404, detail="Workspace not found")

    db.delete(workspace)
    db.commit()
    RetrievalService().delete_workspace(workspace_id)

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
