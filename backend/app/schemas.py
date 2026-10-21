from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from .models import DocumentStatus, EmbeddingStatus, JobState, KnowledgeBaseStatus


class KnowledgeBaseCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str | None = None


class KnowledgeBaseUpdate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)


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
    version_group_id: UUID
    version_number: int
    is_active: bool
    supersedes_id: UUID | None
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
    embedding_status: EmbeddingStatus
    embedding_error: str | None
    version_group_id: UUID | None = None
    version_number: int | None = None


class SearchResult(BaseModel):
    trace_id: UUID
    chunk: ChunkRead
    score: float
    filename: str


class TraceResultRead(BaseModel):
    chunk_id: UUID
    filename: str
    score: float
    rank: int
    lexical_rank: int | None = None
    vector_rank: int | None = None
    vector_distance: float | None = None
    version_group_id: UUID | None = None
    version_number: int | None = None


class QueryTraceRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    knowledge_base_id: UUID
    query: str
    retrieval_mode: str
    embedding_provider: str | None
    embedding_model: str | None
    embedding_dimension: int | None
    result_count: int
    results: list[TraceResultRead]
    created_at: datetime


class RetrievalRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    query: str = Field(min_length=1, max_length=4000)


class RetrievalRead(BaseModel):
    trace: QueryTraceRead
    results: list[SearchResult]


class AnswerRequest(BaseModel):
    query: str = Field(min_length=1, max_length=4000)
    top_k: int = Field(default=6, ge=1, le=12)


class CitationRead(BaseModel):
    chunk_id: UUID
    filename: str
    text: str
    char_start: int
    char_end: int
    heading_path: str | None
    version_group_id: UUID | None = None
    version_number: int | None = None


class AnswerRead(BaseModel):
    trace_id: UUID
    answer: str
    refused: bool
    refusal_reason: str | None
    citations: list[CitationRead]

