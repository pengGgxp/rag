from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from .models import DocumentStatus, JobState, KnowledgeBaseStatus


class KnowledgeBaseCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str | None = None


class KnowledgeBaseRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    description: str | None
    status: KnowledgeBaseStatus
    created_at: datetime
    document_count: int = 0


class JobRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    document_id: UUID
    state: JobState
    error_detail: str | None
    created_at: datetime
    updated_at: datetime


class DocumentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    knowledge_base_id: UUID
    filename: str
    content_type: str
    size_bytes: int
    status: DocumentStatus
    error_detail: str | None
    parsed_text: str | None = None
    parser_name: str | None = None
    created_at: datetime
    job: JobRead | None = None


class ChunkRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    document_id: UUID
    ordinal: int
    text: str
    token_count: int
    char_start: int
    char_end: int
    heading_path: str | None


class SearchResult(BaseModel):
    trace_id: UUID
    chunk: ChunkRead
    score: float
    filename: str

