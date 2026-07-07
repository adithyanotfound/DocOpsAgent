"""Thin wrapper that delegates all logic to DocumentAgentGraph."""
from sqlalchemy.orm import Session

from app.models import AgentRun
from app.services.graph import DocumentAgentGraph


class DocumentAgent:
    def __init__(self, db: Session) -> None:
        self._graph = DocumentAgentGraph(db)

    async def run(
        self,
        workspace_id: str,
        request: str,
        run_id: str,
        attached_image_path: str | None = None,
    ) -> AgentRun:
        return await self._graph.run(
            workspace_id, request, run_id=run_id, attached_image_path=attached_image_path
        )
