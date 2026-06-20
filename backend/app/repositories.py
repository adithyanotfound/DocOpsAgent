from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import DocumentStructure, DocumentVersion, Message, Workspace


class WorkspaceRepository:
    def __init__(self, db: Session):
        self.db = db

    def get(self, workspace_id: str) -> Workspace | None:
        return self.db.get(Workspace, workspace_id)

    def list(self) -> list[Workspace]:
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

    def messages(self, workspace_id: str) -> list[Message]:
        stmt = select(Message).where(Message.workspace_id == workspace_id).order_by(Message.created_at.asc())
        return list(self.db.scalars(stmt))

    def versions(self, workspace_id: str) -> list[DocumentVersion]:
        stmt = (
            select(DocumentVersion)
            .where(DocumentVersion.workspace_id == workspace_id)
            .order_by(DocumentVersion.version_number.asc())
        )
        return list(self.db.scalars(stmt))
