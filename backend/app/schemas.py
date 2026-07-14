from datetime import datetime
from typing import Any

from pydantic import BaseModel


class MessageOut(BaseModel):
    id: str
    role: str
    content: str
    content_parsed: dict[str, Any] | None = None
    created_at: datetime


class VersionOut(BaseModel):
    id: str
    version_number: int
    document_url: str
    pdf_url: str
    created_at: datetime


class KnowledgeDocumentOut(BaseModel):
    id: str
    filename: str
    file_type: str
    file_size_bytes: int
    chunk_count: int
    status: str
    error_message: str | None = None
    created_at: datetime


class WorkspaceOut(BaseModel):
    id: str
    document_type: str
    original_filename: str
    current_version: int
    created_at: datetime
    updated_at: datetime
    messages: list[MessageOut]
    versions: list[VersionOut]
    knowledge_documents: list[KnowledgeDocumentOut] = []


class ChatRequest(BaseModel):
    content: str


class ChatResponse(BaseModel):
    run_id: str
    status: str
