from typing import List
from sqlalchemy import select, func
from sqlalchemy.orm import Session

from app.models import (
    DocumentStructure, DocumentVersion, KnowledgeDocument,
    KnowledgeChunk, Message, Workspace,
)


class WorkspaceRepository:
    def __init__(self, db: Session):
        self.db = db

    def get(self, workspace_id: str) -> Workspace | None:
        return self.db.get(Workspace, workspace_id)

    def list(self) -> List[Workspace]:
        return list(self.db.scalars(select(Workspace).order_by(Workspace.updated_at.desc())))

    def latest_version(self, workspace_id: str) -> DocumentVersion | None:
        stmt = (
            select(DocumentVersion)
            .where(DocumentVersion.workspace_id == workspace_id)
            .order_by(DocumentVersion.version_number.desc())
        )
        return self.db.scalars(stmt).first()

    def version(self, workspace_id: str, version_number: int) -> DocumentVersion | None:
        stmt = select(DocumentVersion).where(
            DocumentVersion.workspace_id == workspace_id,
            DocumentVersion.version_number == version_number,
        )
        return self.db.scalars(stmt).first()

    def structure(self, workspace_id: str, version_number: int) -> DocumentStructure | None:
        stmt = select(DocumentStructure).where(
            DocumentStructure.workspace_id == workspace_id,
            DocumentStructure.version_number == version_number,
        )
        return self.db.scalars(stmt).first()

    def messages(self, workspace_id: str) -> List[Message]:
        stmt = select(Message).where(Message.workspace_id == workspace_id).order_by(Message.created_at.asc())
        return list(self.db.scalars(stmt))

    def versions(self, workspace_id: str) -> List[DocumentVersion]:
        stmt = (
            select(DocumentVersion)
            .where(DocumentVersion.workspace_id == workspace_id)
            .order_by(DocumentVersion.version_number.asc())
        )
        return list(self.db.scalars(stmt))

    # ── Knowledge Base ──────────────────────────────────────────────────────

    def list_knowledge_documents(self, workspace_id: str) -> List[KnowledgeDocument]:
        stmt = (
            select(KnowledgeDocument)
            .where(KnowledgeDocument.workspace_id == workspace_id)
            .order_by(KnowledgeDocument.created_at.asc())
        )
        return list(self.db.scalars(stmt))

    def get_knowledge_document(self, document_id: str) -> KnowledgeDocument | None:
        return self.db.get(KnowledgeDocument, document_id)

    def count_knowledge_documents(self, workspace_id: str) -> int:
        stmt = (
            select(func.count())
            .select_from(KnowledgeDocument)
            .where(KnowledgeDocument.workspace_id == workspace_id)
        )
        return self.db.scalar(stmt) or 0

    def total_knowledge_size_bytes(self, workspace_id: str) -> int:
        stmt = (
            select(func.sum(KnowledgeDocument.file_size_bytes))
            .where(KnowledgeDocument.workspace_id == workspace_id)
        )
        return self.db.scalar(stmt) or 0

    def list_knowledge_chunks(self, workspace_id: str) -> List[KnowledgeChunk]:
        stmt = (
            select(KnowledgeChunk)
            .where(KnowledgeChunk.workspace_id == workspace_id)
            .order_by(KnowledgeChunk.document_id, KnowledgeChunk.chunk_index)
        )
        return list(self.db.scalars(stmt))
