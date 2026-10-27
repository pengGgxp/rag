from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4
from fastapi import HTTPException
from sqlalchemy import select, func
from sqlalchemy.orm import Session
from app.models import SiteModelProvider, LLMProviderConfig, UsagePolicy, Workspace, ModelBillingSettings
from app.shared_providers import select_model
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
            row = db.get(Workspace, workspace_id, with_for_update=True, populate_existing=True)
            if row.model_version != 0:
                return 409
            select_model(db, workspace_id, provider_id)
            db.commit()
            return 200
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(choose, range(4)))
    assert results.count(200) == 1 and results.count(409) == 3
    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(LLMProviderConfig).where(LLMProviderConfig.workspace_id == workspace_id)) == 1
        assert db.scalar(select(func.count()).select_from(UsagePolicy).where(UsagePolicy.workspace_id == workspace_id)) == 0
        provider = db.get(SiteModelProvider, provider_id)
        provider.prices = {key: {**value, "credits_per_unit": 99} for key, value in provider.prices.items()}
        db.commit()
        select_model(db, workspace_id, provider_id); db.commit()
        assert not db.scalars(select(UsagePolicy).where(UsagePolicy.workspace_id == workspace_id)).all()
        from app.model_billing import price_snapshot
        binding = db.scalar(select(LLMProviderConfig).where(LLMProviderConfig.workspace_id == workspace_id))
        assert price_snapshot(db, workspace_id, binding.id)["llm_input_tokens"]["credits_per_unit"] == 99
