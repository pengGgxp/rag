"""Validated model protocols and explicit image specifications."""
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator
from .credit_amounts import CreditRate

ChatProtocol = Literal["chat_completions", "responses", "anthropic", "gemini"]


class ImageSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    operation: Literal["generate", "edit"]
    size: str = Field(min_length=1, max_length=40, pattern=r"^[A-Za-z0-9_:.-]+$")
    quality: str = Field(min_length=1, max_length=40, pattern=r"^[A-Za-z0-9_.-]+$")
    credits_per_image: CreditRate
    cost_per_image: CreditRate | None = None


class ImageConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    protocol: Literal["openai_images", "gemini", "responses"] = "openai_images"
    image_model: str | None = Field(default=None, min_length=1, max_length=200)
    generate: bool = False
    edit: bool = False
    max_count: int = Field(default=1, ge=1, le=4, strict=True)
    max_references: int = Field(default=1, ge=1, le=8, strict=True)
    default_size: str = "1024x1024"
    default_quality: str = "standard"
    specs: list[ImageSpec] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def validate_specs(self):
        if self.protocol == "responses" and (self.generate or self.edit) and not self.image_model:
            raise ValueError("Responses 生图需要明确指定图片工具模型")
        keys = [(s.operation, s.size, s.quality) for s in self.specs]
        if len(keys) != len(set(keys)):
            raise ValueError("图片规格重复")
        for operation, enabled in (("generate", self.generate), ("edit", self.edit)):
            if enabled and (operation, self.default_size, self.default_quality) not in keys:
                raise ValueError("请为每项启用能力配置默认规格及价格，免费须明确填写 0")
        if self.protocol in ("responses", "gemini") and self.max_count != 1:
            raise ValueError("原生图片任务每次生成一张图片")
        return self
