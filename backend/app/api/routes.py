import shutil
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.repositories import WorkspaceRepository
from app.schemas import ChatRequest, ChatResponse, WorkspaceOut
from app.services.agent import DocumentAgent
from app.services.serializers import serialize_workspace
from app.services.uploads import UploadService
from app.services.websocket_manager import manager

router = APIRouter()


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


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


@router.post("/workspaces/{workspace_id}/chat", response_model=ChatResponse)
async def chat(workspace_id: str, payload: ChatRequest, db: Session = Depends(get_db)) -> ChatResponse:
    repo = WorkspaceRepository(db)
    if not repo.get(workspace_id):
        raise HTTPException(status_code=404, detail="Workspace not found")
    run = await DocumentAgent(db).run(workspace_id, payload.content)
    return ChatResponse(run_id=run.id, status=run.status)


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

    # Delete all DB rows (messages, versions, structures cascade automatically).
    db.delete(workspace)
    db.commit()

    # Delete embeddings from vector DB.
    RetrievalService().delete_workspace(workspace_id)

    # Remove files from disk.
    workspace_dir = settings.storage_root / workspace_id
    if workspace_dir.exists():
        shutil.rmtree(workspace_dir)



@router.get("/files/{workspace_id}/{filename}")
def get_file(workspace_id: str, filename: str) -> FileResponse:
    from app.core.config import settings

    path = settings.storage_root / workspace_id / filename
    if not path.exists():
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(path)


@router.get("/source-files/{workspace_id}/{filename}")
def get_source_file(workspace_id: str, filename: str) -> FileResponse:
    """
    Serve source .docx/.pptx files so OnlyOffice can fetch them during conversion.
    OnlyOffice pulls files by URL — this endpoint is that URL.
    """
    from app.core.config import settings

    path = (settings.storage_root / workspace_id / filename).resolve()
    storage = settings.storage_root.resolve()

    # Safety: only serve files inside storage_root
    if not str(path).startswith(str(storage)):
        raise HTTPException(status_code=403, detail="Access denied")
    if not path.exists():
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(path)


@router.websocket("/workspaces/{workspace_id}/ws")
async def workspace_ws(websocket: WebSocket, workspace_id: str) -> None:
    await manager.connect(workspace_id, websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(workspace_id, websocket)
