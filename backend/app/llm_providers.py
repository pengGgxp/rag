"""Shared provider credential helpers used by site-managed model bindings."""
from urllib.parse import urlparse

from fastapi import HTTPException
from . import mcp_client
from .llm import ChatProviderConfig


def validate_base_url(value):
    parsed = urlparse(value.strip())
    if parsed.scheme not in ("http", "https") or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise HTTPException(422, "供应商地址必须是不含凭据、查询参数的 HTTP 或 HTTPS URL")
    return value.strip().rstrip("/")


def _encrypt_api_key(value):
    try:
        return mcp_client.encrypt_headers({"api_key": value})
    except mcp_client.MCPError as exc:
        raise HTTPException(503, "尚未配置服务端凭据加密密钥") from exc


def _decrypt_api_key(value):
    return mcp_client.decrypt_headers(value).get("api_key", "")


def request_provider_config(db, account_id, site_provider_id, thinking_option_id=None, thinking_config_version=None):
    from .shared_providers import request_binding
    from .model_connections import lock_connection
    from .thinking import select_thinking
    row, shared = request_binding(db, account_id, site_provider_id)
    connection = lock_connection(db, shared.connection_id)
    thinking = select_thinking(shared.thinking_config, shared.thinking_version,
                              thinking_option_id, thinking_config_version)
    from .llm_responses import validate_thinking
    try:
        validate_thinking(shared.chat_protocol, thinking)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    try:
        api_key = _decrypt_api_key(connection.encrypted_api_key)
    except Exception as exc:
        raise HTTPException(503, "模型服务尚未就绪，请联系站点管理员") from exc
    if not api_key:
        raise HTTPException(503, "模型服务尚未就绪，请联系站点管理员")
    return ChatProviderConfig(base_url=connection.base_url, api_key=api_key, model=shared.model,
        timeout_seconds=connection.timeout_seconds, provider_id=str(row.id), provider_key=row.key,
        provider_name=shared.name, site_provider_id=str(shared.id), thinking=thinking,
        context_capacity=shared.context_capacity, max_output_tokens=shared.max_output_tokens,
        config_version=shared.version, connection_id=str(connection.id), connection_version=connection.version,
        api_protocol=shared.chat_protocol, supports_images=shared.supports_images)
