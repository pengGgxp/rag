from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ExtensionSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    skill_ids: list[UUID] = Field(default_factory=list)
    mcp_server_ids: list[UUID] = Field(default_factory=list)
    question_workbench: bool = False


class ConversationCreate(ExtensionSelection):
    model_config = ConfigDict(str_strip_whitespace=True)
    title: str = Field(default="新对话", min_length=1, max_length=200)


class ConversationRename(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    title: str | None = Field(default=None, min_length=1, max_length=200)
    pinned: bool | None = None
    revision: int = Field(ge=1)


class ContextRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    request_id: UUID
    provider_id: UUID
    thinking_option_id: str | None = Field(default=None, min_length=1, max_length=40)
    thinking_config_version: int | None = Field(default=None, ge=0, strict=True)


class InputReferences(BaseModel):
    attachment_ids: list[UUID] = Field(default_factory=list, max_length=6)
    document_ids: list[UUID] = Field(default_factory=list, max_length=8)
    knowledge_base_ids: list[UUID] = Field(default_factory=list, max_length=8)
    resource_ids: list[UUID] = Field(default_factory=list, max_length=8)


class TurnRequest(ContextRequest, InputReferences):
    web_search: bool = True
    query: str | None = Field(default=None, min_length=1)
    retry_message_id: UUID | None = None

    @model_validator(mode="after")
    def one_operation(self):
        if bool(self.query or self.attachment_ids) == bool(self.retry_message_id):
            raise ValueError("Provide either query or retry_message_id")
        return self


class StopRequest(BaseModel):
    request_id: UUID


class SteerRequest(InputReferences):
    model_config = ConfigDict(str_strip_whitespace=True)
    message_id: UUID
    text: str = Field(default="", max_length=100_000)

    @model_validator(mode="after")
    def has_input(self):
        if not self.text and not self.attachment_ids:
            raise ValueError("Provide text or an attachment")
        return self


Category = Literal["missing_source", "retrieval_error", "answer_error", "citation_error"]


class FeedbackWrite(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    message_id: UUID
    rating: Literal["up", "down"]
    categories: list[Category] = Field(default_factory=list, max_length=4)
    comment: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def validate_details(self):
        if len(set(self.categories)) != len(self.categories):
            raise ValueError("问题类型不能重复")
        if self.rating == "down" and not self.categories:
            raise ValueError("点踩时请选择至少一个问题类型")
        if self.rating == "up" and (self.categories or self.comment):
            raise ValueError("点赞不需要问题类型或说明")
        return self
