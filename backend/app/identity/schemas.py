"""身份认证和站点账号管理的 HTTP 请求模型。"""
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field, SecretStr, field_validator

from ..credit_amounts import CreditRate


class Credentials(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)


class Registration(Credentials):
    model_config = ConfigDict(str_strip_whitespace=False)
    password: str = Field(min_length=12, max_length=128)
    name: str = Field(min_length=1, max_length=100)


class Bootstrap(Registration):
    token: str = Field(min_length=20, max_length=200)


class PasswordChange(BaseModel):
    current_password: str = Field(default="", max_length=128)
    password: str = Field(min_length=12, max_length=128)


class EmailRequest(BaseModel):
    email: EmailStr


class ResetPassword(BaseModel):
    token: str = Field(min_length=20, max_length=200)
    password: str = Field(min_length=12, max_length=128)


class OAuthStart(BaseModel):
    link: bool = False


class SendCode(BaseModel):
    email: EmailStr | None = None
    purpose: Literal["register", "login", "verify", "wechat"] = "login"
    return_to: str = "/"


class VerifyCode(BaseModel):
    code: str = Field(pattern=r"^\d{6}$")


class ConfirmEmail(BaseModel):
    token: str = Field(min_length=20, max_length=200)


class CompleteRegistration(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    password: str | None = Field(default=None, max_length=128)


class WechatStart(BaseModel):
    link: bool = False
    return_to: str = "/"


class SMTPInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    host: str = Field(default="", max_length=255, pattern=r"^[a-zA-Z0-9.:-]*$")
    port: int = Field(default=587, ge=1, le=65535)
    tls_mode: Literal["ssl", "starttls", "plain"] = "starttls"
    username: str = Field(default="", max_length=320)
    password: SecretStr | None = None
    clear_password: bool = False
    from_address: str = Field(default="", max_length=320)
    from_name: str = Field(default="知库", max_length=100, pattern=r"^[^\r\n]*$")


class WechatInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    app_id: str = Field(default="", max_length=100, pattern=r"^[a-zA-Z0-9_-]*$")
    app_key: SecretStr | None = None
    clear_app_key: bool = False


class PolicyInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    require_verified_email: bool = False
    hourly_email_limit: int = Field(default=100, ge=1, le=10000)


class ConfigInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    smtp: SMTPInput | None = None
    wechat: WechatInput | None = None
    policy: PolicyInput | None = None


class TestEmail(BaseModel):
    email: EmailStr


class CreditAdjustment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID
    direction: Literal["add", "deduct"]
    amount: CreditRate = Field(gt=0)
    expected_balance: CreditRate
    reason: str = Field(min_length=1, max_length=200)

    @field_validator("reason")
    @classmethod
    def nonempty_reason(cls, value):
        value = value.strip()
        if not value:
            raise ValueError("请填写调整原因")
        return value


class UserStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")
    suspended: bool
    reason_code: str = Field(default="support_review", pattern=r"^(support_review|abuse|security|resolved)$")


class WorkbenchAccess(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool
    version: int = Field(ge=0)


class UserModelPolicyUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    available_models: list[UUID] = Field(max_length=100)
    default_model: UUID | None = None
    billing_multiplier_percent: int = Field(ge=1, le=10000, strict=True)
    version: int = Field(ge=0, strict=True)
