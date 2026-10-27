from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event
from uuid import uuid4
from fastapi import HTTPException
from sqlalchemy import select, func, event, text
from sqlalchemy.orm import Session
from app.models import SiteModelProvider, LLMProviderConfig, UsagePolicy, Workspace, ModelBillingSettings
from app.shared_providers import request_binding
from app.model_billing import CAPABILITIES
from test_auth_postgres import auth_postgres


def test_parallel_selection_has_one_association_and_no_price_copies(auth_postgres):
    engine = auth_postgres
    with Session(engine) as db:
        settings = db.get(ModelBillingSettings, 1)
        if settings is None:
            settings = ModelBillingSettings(id=1)
            db.add(settings)
        settings.shared_pricing_enabled = True
        workspace = Workspace(name="Selection concurrency")
        provider = SiteModelProvider(key=f"pg-{uuid4()}", name="Shared", model="test", base_url="https://example.test/v1",
            encrypted_api_key="not-used", enabled=True, published=True, prices={key: {"capability": key,
                "unit": "tokens", "unit_size": 1000, "credits_per_unit": 7, "enabled": True} for key in CAPABILITIES})
        db.add_all([workspace, provider]); db.commit()
        workspace_id, provider_id = workspace.id, provider.id
    barrier = Barrier(4)
    def choose(_):
        with Session(engine) as db:
            barrier.wait(timeout=10)
            request_binding(db, workspace_id, provider_id)
            db.commit()
            return 200
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(choose, range(4)))
    assert results == [200] * 4
    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(LLMProviderConfig).where(LLMProviderConfig.workspace_id == workspace_id)) == 1
        assert db.scalar(select(func.count()).select_from(UsagePolicy).where(UsagePolicy.workspace_id == workspace_id)) == 0
        provider = db.get(SiteModelProvider, provider_id)
        provider.prices = {key: {**value, "credits_per_unit": 99} for key, value in provider.prices.items()}
        db.commit()
        request_binding(db, workspace_id, provider_id); db.commit()
        assert not db.scalars(select(UsagePolicy).where(UsagePolicy.workspace_id == workspace_id)).all()
        from app.model_billing import price_snapshot
        binding = db.scalar(select(LLMProviderConfig).where(LLMProviderConfig.workspace_id == workspace_id))
        assert price_snapshot(db, workspace_id, binding.id)["llm_input_tokens"]["credits_per_unit"] == 99


def test_request_and_administrator_use_same_configuration_lock_order(auth_postgres):
    engine = auth_postgres
    with Session(engine) as db:
        if db.get(ModelBillingSettings, 1) is None:
            db.add(ModelBillingSettings(id=1))
        workspace = Workspace(name="Concurrent model edit")
        provider = SiteModelProvider(key=f"edit-{uuid4()}", name="Shared", model="before",
            base_url="https://example.test/v1", encrypted_api_key="not-used", enabled=True,
            published=True, prices={key: {"capability": key, "unit": "tokens", "unit_size": 1000,
                "credits_per_unit": 1, "enabled": True} for key in CAPABILITIES})
        db.add_all([workspace, provider])
        db.commit()
        workspace_id, provider_id = workspace.id, provider.id
    waiting = Event()

    def before_query(_conn, _cursor, statement, _parameters, _context, _many):
        if "model_billing_settings" in statement and "FOR SHARE" in statement:
            waiting.set()

    def request():
        with Session(engine) as db:
            _, provider = request_binding(db, workspace_id, provider_id)
            model = provider.model
            db.commit()
            return model

    event.listen(engine, "before_cursor_execute", before_query)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            with Session(engine) as admin:
                admin.execute(text("SET LOCAL lock_timeout = '2s'"))
                admin.get(ModelBillingSettings, 1, with_for_update=True)
                future = pool.submit(request)
                assert waiting.wait(timeout=5)
                # Request admission waits on settings, without holding the provider.
                provider = admin.get(SiteModelProvider, provider_id, with_for_update=True)
                provider.model = "after"
                admin.commit()
            assert future.result(timeout=5) == "after"
    finally:
        event.remove(engine, "before_cursor_execute", before_query)
