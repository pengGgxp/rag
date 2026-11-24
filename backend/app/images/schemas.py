"""图片任务与管理接口的请求模型。"""
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Reference(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: UUID
    version: int = Field(ge=1, strict=True)


class ImageInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation: str = Field(pattern="^(generate|edit)$", default="generate")
    prompt: str = Field(min_length=1, max_length=12000)
    size: str | None = Field(default=None, max_length=40)
    quality: str | None = Field(default=None, max_length=40)
    count: int = Field(default=1, ge=1, le=4, strict=True)
    references: list[Reference] = Field(default_factory=list, max_length=8)

    @model_validator(mode="after")
    def valid_operation(self):
        if not self.prompt.strip() or (self.operation == "edit") != bool(self.references):
            raise ValueError("编辑图片需要参考资源，生成图片不能携带参考资源")
        return self


class CreateInput(ImageInput):
    idempotency_key: UUID


class SettingsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int = Field(ge=0)
    generate_model_id: UUID | None = None
    edit_model_id: UUID | None = None


class Resolution(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirmed_images: int = Field(ge=0, le=4, strict=True)
    reason: str = Field(min_length=5, max_length=300)
