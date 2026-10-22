from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str = "postgresql+psycopg://rag:rag@localhost:5432/rag"
    redis_url: str = "redis://localhost:6379/0"
    s3_endpoint: str = "http://localhost:9000"
    s3_access_key: str = "minioadmin"
    s3_secret_key: str = "minioadmin"
    s3_bucket: str = "rag-documents"
    s3_region: str = "us-east-1"
    embedding_base_url: str | None = None
    embedding_api_key: SecretStr | None = None
    embedding_model: str | None = None
    embedding_dimension: int = 1024
    embedding_timeout_seconds: float = 30.0
    llm_base_url: str | None = None
    llm_api_key: SecretStr | None = None
    llm_model: str | None = None
    llm_timeout_seconds: float = 60.0
    mcp_credential_key: SecretStr | None = None
    mcp_allowed_private_hosts: str = ""
    mcp_timeout_seconds: float = Field(default=60, gt=0)
    pdf_parser: Literal["local", "aliyun"] = "local"
    aliyun_access_key_id: SecretStr | None = None
    aliyun_access_key_secret: SecretStr | None = None
    docmind_max_pages: int = Field(default=50, ge=1, le=500)
    docmind_timeout_seconds: float = Field(default=300, ge=1, le=1800)
    docmind_poll_seconds: float = Field(default=3, ge=0.1, le=30)
    frontend_url: str = "http://localhost:3000"
    api_public_url: str = "http://localhost:8000"
    auth_cookie_secure: bool = False
    auth_session_hours: int = 168
    auth_secret_key: SecretStr | None = None
    oidc_name: str = "企业账号"
    oidc_metadata_url: str | None = None
    oidc_client_id: str | None = None
    oidc_client_secret: SecretStr | None = None
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_username: str | None = None
    smtp_password: SecretStr | None = None
    smtp_from: str | None = None
    smtp_starttls: bool = True
    zhifux_enabled: bool = False
    zhifux_api_base_url: str = ""
    zhifux_merchant_num: str = ""
    zhifux_secret: SecretStr | None = None
    zhifux_pay_type: str = ""
    recharge_packages_json: str = "[]"
    usage_authorization_hold_credits: int = Field(default=1, ge=1, le=100000)

    @property
    def oidc_configured(self) -> bool:
        return bool(self.oidc_metadata_url and self.oidc_client_id and self.oidc_client_secret
                    and self.auth_secret_key and len(self.auth_secret_key.get_secret_value()) >= 32)

    @property
    def mail_configured(self) -> bool:
        return bool(self.smtp_host and self.smtp_from)

    @property
    def embedding_configured(self) -> bool:
        return bool(self.embedding_base_url and self.embedding_api_key and self.embedding_model)

    @property
    def llm_configured(self) -> bool:
        return bool(self.llm_base_url and self.llm_api_key and self.llm_model)

    @property
    def docmind_configured(self) -> bool:
        return bool(self.aliyun_access_key_id and self.aliyun_access_key_secret)

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


@lru_cache
def get_settings() -> Settings:
    return Settings()

