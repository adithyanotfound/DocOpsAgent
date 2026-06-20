import json

from app.models import DocumentVersion, Workspace
from app.repositories import WorkspaceRepository
from app.schemas import MessageOut, VersionOut, WorkspaceOut


def file_url(version: DocumentVersion, path_value: str) -> str:
    return f"/api/files/{version.workspace_id}/{path_value.split('/')[-1]}"


def _parse_content(content: str) -> dict | None:
    """Try to parse message content as structured JSON. Returns None for plain text."""
    try:
        parsed = json.loads(content)
        if isinstance(parsed, dict) and parsed.get("type") == "agent_response":
            return parsed
    except Exception:
        pass
    return None


def serialize_workspace(workspace: Workspace, repo: WorkspaceRepository) -> WorkspaceOut:
    versions = repo.versions(workspace.id)
    messages = repo.messages(workspace.id)
    return WorkspaceOut(
        id=workspace.id,
        document_type=workspace.document_type,
        original_filename=workspace.original_filename,
        current_version=workspace.current_version,
        created_at=workspace.created_at,
        updated_at=workspace.updated_at,
        messages=[
            MessageOut(
                id=m.id,
                role=m.role,
                content=m.content,
                content_parsed=_parse_content(m.content),
                created_at=m.created_at,
            )
            for m in messages
        ],
        versions=[
            VersionOut(
                id=v.id,
                version_number=v.version_number,
                document_url=file_url(v, v.document_path),
                pdf_url=file_url(v, v.pdf_path),
                created_at=v.created_at,
            )
            for v in versions
        ],
    )
