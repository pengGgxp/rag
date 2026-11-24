pytest_plugins = ["test_auth"]

import pytest


@pytest.fixture(autouse=True)
def isolated_conversation_queue(monkeypatch):
    """Tests run durable jobs explicitly; never send jobs to a developer's broker."""
    from app.integrations import mcp_client
    from app import tasks
    from app.conversations import jobs as conversation_jobs
    from cryptography.fernet import Fernet
    from pydantic import SecretStr
    settings = mcp_client.get_settings()
    if not (settings.mcp_credential_key or settings.site_config_encryption_key):
        configured = settings.model_copy(update={"mcp_credential_key": SecretStr(Fernet.generate_key().decode())})
        monkeypatch.setattr(mcp_client, "get_settings", lambda: configured)
    monkeypatch.setattr(conversation_jobs, "dispatch", lambda _identifier: None)
    monkeypatch.setattr(tasks.name_conversation, "delay", lambda _identifier: None)


@pytest.fixture(autouse=True)
def isolated_deployment_state(monkeypatch):
    """Unit tests never query a developer's maintenance database."""
    from app.operations import deployment
    monkeypatch.setattr(deployment, "is_active", lambda: False)
