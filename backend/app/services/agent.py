"""Thin wrapper that delegates all logic to DocumentAgentGraph."""
from sqlalchemy.orm import Session

from app.models import AgentRun
from app.services.graph import DocumentAgentGraph


class DocumentAgent:
    def __init__(self, db: Session) -> None:
        self._graph = DocumentAgentGraph(db)

    async def run(self, workspace_id: str, request: str) -> AgentRun:
        return await self._graph.run(workspace_id, request)
