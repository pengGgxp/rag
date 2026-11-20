"""Shared provider credential helpers used by site-managed model bindings."""
from urllib.parse import urlparse
from uuid import UUID

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


def refresh_chat_credentials(db, config):
    """Accept a rotated key only for the pinned model binding and endpoint."""
    from dataclasses import replace
    from .models import ModelConnection, SiteModelProvider
    if not config.site_provider_id or not config.connection_id:
        return None
    model = db.get(SiteModelProvider, UUID(config.site_provider_id))
    connection = db.get(ModelConnection, UUID(config.connection_id))
    if (not model or not model.enabled or not connection or not connection.enabled
            or model.connection_id != connection.id or model.version != config.config_version
            or connection.base_url != config.base_url or connection.version <= config.connection_version):
        return None
    try:
        key = _decrypt_api_key(connection.encrypted_api_key)
    except (mcp_client.MCPError, TypeError, ValueError):
        return None
    if not key or key == config.api_key:
        return None
    return replace(config, api_key=key, connection_version=connection.version)


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
        api_protocol=shared.chat_protocol, supports_images=shared.supports_images,
        responses_websocket=shared.chat_protocol == "responses" and connection.platform == "openai"
        and connection.base_url.rstrip("/") in ("https://api.openai.com/v1", "https://api.openai.com/v1/responses"))
