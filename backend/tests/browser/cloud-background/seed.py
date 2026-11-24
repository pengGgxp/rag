from decimal import Decimal
from pathlib import Path
import json

from app import cloud_computer as cloud

from app.identity import auth
from app.credits import ensure_account
from app.db import SessionLocal
from app.llm_providers import _encrypt_api_key
from app.models import CloudComputerSettings, ModelBillingSettings, ModelConnection, SiteModelProvider
from app.identity.site_auth import protect

with SessionLocal() as db:
    assert db.bind.url.database == "rag_auth_test_issue118_browser"
    user = auth.create_user(db, "issue118-local@example.com", "Issue118 Acceptance", "Pr117-local-only-2026!")
    workspace = user.id
    user.email_verified_at = auth.now()
    user.is_site_admin = True
    account = ensure_account(db, workspace)
    account.balance = account.lifetime_granted = Decimal("1000")
    connection = ModelConnection(name="Issue118 synthetic upstream", platform="custom",
        base_url="http://mock:8900/v1", encrypted_api_key=_encrypt_api_key("synthetic-only"))
    db.add(connection)
    db.flush()
    prices = {key: {"capability": key, "unit": "tokens", "unit_size": 1000,
                   "credits_per_unit": 0, "enabled": True} for key in
              ("llm_input_tokens", "llm_cached_input_tokens", "llm_output_tokens")}
    provider = SiteModelProvider(key="issue118-local", name="Issue118 Synthetic Model", model="synthetic",
        connection_id=connection.id, enabled=True, prices=prices, context_capacity=128000, max_output_tokens=2048)
    db.add(provider)
    db.flush()
    db.merge(ModelBillingSettings(id=1, enabled=True, shared_pricing_enabled=True, default_provider_id=provider.id))
    db.add(CloudComputerSettings(id=1, version=1, enabled=True,
        config=cloud.Configuration(region="cn-hangzhou", template="synthetic-only", specification="local",
            credits_per_minute="0.6", idle_seconds=600, max_seconds=3600,
            oss_role_arn="acs:ram::123:role/test").model_dump(mode="json"),
        encrypted_api_key=protect("synthetic-only")))
    db.commit()
    Path("/acceptance/namespace.json").write_text(json.dumps({
        "user_id": str(user.id), "workspace_id": str(workspace), "provider_id": str(provider.id)}))
    print("Created temporary user/workspace with synthetic provider and 1000 local credits.")
