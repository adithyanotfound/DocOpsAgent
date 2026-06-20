from pathlib import Path

from fastapi import UploadFile
from sqlalchemy.orm import Session

from app.models import DocumentStructure, DocumentVersion, Message, Workspace
from app.services.document_processor import DocumentProcessor
from app.services.preview import PreviewService
from app.services.retrieval import RetrievalService
from app.services.storage import StorageService


class UploadService:
    def __init__(self, db: Session):
        self.db = db
        self.storage = StorageService()
        self.processor = DocumentProcessor()
        self.preview = PreviewService()
        self.retrieval = RetrievalService()

    async def create_workspace(self, file: UploadFile) -> Workspace:
        document_type = Path(file.filename or "").suffix.lower().replace(".", "")
        workspace = Workspace(document_type=document_type, original_filename=file.filename or "document")
        self.db.add(workspace)
        self.db.flush()

        document_path = self.storage.version_document_path(workspace.id, 1, document_type)
        with document_path.open("wb") as handle:
            while chunk := await file.read(1024 * 1024):
                handle.write(chunk)

        pdf_path = self.storage.version_pdf_path(workspace.id, 1)
        self.preview.convert_to_pdf(document_path, pdf_path)
        structure = self.processor.extract(document_path, document_type)

        self.db.add(
            DocumentVersion(
                workspace_id=workspace.id,
                version_number=1,
                document_path=str(document_path),
                pdf_path=str(pdf_path),
            )
        )
        self.db.add(
            DocumentStructure(
                workspace_id=workspace.id,
                version_number=1,
                structure_json=structure,
            )
        )
        self.db.add(Message(workspace_id=workspace.id, role="assistant", content="Document uploaded and indexed."))
        self.db.commit()
        self.db.refresh(workspace)
        # Index blocks into Qdrant for semantic retrieval (no-op if not configured).
        self.retrieval.index_workspace(workspace.id, structure)
        return workspace
