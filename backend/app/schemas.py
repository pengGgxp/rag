from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .models import DocumentStatus, EmbeddingStatus, JobState, KnowledgeBaseStatus


class KnowledgeBaseCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str | None = None
    tags: list[str] = Field(default_factory=list, max_length=12)

    @field_validator("tags")
    @classmethod
    def normalize_tags(cls, value: list[str]) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()
        for raw in value:
            tag = raw.strip()
            if not tag or len(tag) > 40:
                raise ValueError("标签不能为空且不能超过 40 个字符")
            key = tag.casefold()
            if key not in seen:
                seen.add(key)
                result.append(tag)
        return result


class KnowledgeBaseUpdate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    tags: list[str] | None = Field(default=None, max_length=12)

    @field_validator("tags")
    @classmethod
    def normalize_tags(cls, value: list[str] | None) -> list[str] | None:
        return KnowledgeBaseCreate.normalize_tags(value) if value is not None else None


class KnowledgeBaseRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    directory_path: str | None = None
    description: str | None
    tags: list[str]
    status: KnowledgeBaseStatus
    created_at: datetime
    document_count: int = 0


class IngestionRetryProgress(BaseModel):
    number: int
    maximum: int


class DocumentFailureRead(BaseModel):
    code: str
    stage: str
    message: str
    retryable: bool
    request_id: str


class JobRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    document_id: UUID
    state: JobState
    error_detail: str | None
    retry_progress: IngestionRetryProgress | None = None
    failure: DocumentFailureRead | None = None
    phase: str | None = None
    retry_after: datetime | None = None
    created_at: datetime
    updated_at: datetime


class DocumentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    personal_file_id: UUID | None = None
    knowledge_base_id: UUID
    version_group_id: UUID
    version_number: int
    is_active: bool
    supersedes_id: UUID | None
    filename: str
    content_origin: str = "uploaded"
    content_type: str
    size_bytes: int
    status: DocumentStatus
    error_detail: str | None
    failure: DocumentFailureRead | None = None
    parser_name: str | None = None
    created_at: datetime
    job: JobRead | None = None


class DocumentPreviewRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    knowledge_base_id: UUID
    version_group_id: UUID
    version_number: int
    is_active: bool
    filename: str
    content_origin: str = "uploaded"
    content_type: str
    size_bytes: int
    status: DocumentStatus
    error_detail: str | None
    failure: DocumentFailureRead | None = None
    parser_name: str | None
    parsed_text: str | None
    created_at: datetime


class SourceLocation(BaseModel):
    page_number: int
    char_start: int
    char_end: int
    block_type: str
    bbox: list[float] | None = None


class ChunkRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    document_id: UUID
    ordinal: int
    content_origin: str = "uploaded"
    text: str
    token_count: int
    char_start: int
    char_end: int
    heading_path: str | None
    page_number: int | None = None
    source_locations: list[SourceLocation] = Field(default_factory=list)
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
    bm25_score: float | None = None
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
    document_ids: list[UUID] | None = Field(default=None, max_length=8)


class RetrievalRead(BaseModel):
    trace: QueryTraceRead
    results: list[SearchResult]


class AnswerRequest(BaseModel):
    provider_id: UUID
    thinking_option_id: str | None = Field(default=None, min_length=1, max_length=40)
    thinking_config_version: int | None = Field(default=None, ge=0, strict=True)
    query: str = Field(min_length=1, max_length=4000)
    top_k: int = Field(default=6, ge=1, le=12)


class CitationRead(BaseModel):
    chunk_id: UUID
    filename: str
    content_origin: str = "uploaded"
    text: str
    char_start: int
    char_end: int
    heading_path: str | None
    page_number: int | None = None
    source_locations: list[SourceLocation] = Field(default_factory=list)
    version_group_id: UUID | None = None
    version_number: int | None = None


class AnswerRead(BaseModel):
    provider_id: UUID | None = None
    provider_name: str | None = None
    model: str | None = None
    thinking: dict | None = None
    trace_id: UUID
    answer: str
    refused: bool
    refusal_reason: str | None
    citations: list[CitationRead]

