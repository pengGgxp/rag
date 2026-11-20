"""Disposable local API fixture. No deployed database, payment or model calls."""
import json
import os
from pathlib import Path
import sys
import tempfile
from contextlib import asynccontextmanager

os.environ["FRONTEND_URL"] = "http://127.0.0.1:3110"
os.environ["DATABASE_URL"] = f"sqlite:///{Path(tempfile.mkdtemp(prefix='rag-pricing47-')) / 'preview.db'}"
os.environ["SITE_CONFIG_ENCRYPTION_KEY"] = "local-fixture-only"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import auth, main, payments
from app.config import Settings
from app.db import Base, SessionLocal, engine
from app.models import ModelBillingSettings, SiteModelProvider
from app.model_pricing import TEMPLATE_CAPABILITIES
from auth_helpers import authenticate, configured_model
from fastapi.testclient import TestClient
from pydantic import SecretStr
import uvicorn


if __name__ == "__main__":
    Base.metadata.create_all(engine)
    settings = Settings(_env_file=None).model_copy(update={
        "zhifux_enabled": True, "zhifux_api_base_url": "https://gateway.example/api",
        "zhifux_merchant_num": "888888", "zhifux_secret": SecretStr("local-fixture-only"),
        "zhifux_pay_type": "aloop", "frontend_url": "http://127.0.0.1:3110",
        "api_public_url": "https://api.example", "recharge_packages_json": json.dumps([
            {"id": "regular", "name": "普通套餐", "amount_fen": 1000, "credits": 9000},
            {"id": "bonus", "name": "优惠套餐", "amount_fen": 1000, "credits": 10000},
        ])})
    # Catalog validation requires HTTPS production URLs; the browser origin remains local.
    settings.frontend_url = "https://app.example"
    payments.get_settings = lambda: settings
    @asynccontextmanager
    async def lifespan(_):
        yield
    main.app.router.lifespan_context = lifespan
    with SessionLocal() as db:
        client = TestClient(main.app)
        user = authenticate(client, db)
        user.email_verified_at, user.is_site_admin = auth.now(), True
        binding = configured_model(db, user.id)
        model = db.get(SiteModelProvider, binding.site_provider_id)
        model.name = "定价验收模型"
        template = {key: {"capability": key, "unit": "pages" if key == "ocr_pages" else "tokens",
            "unit_size": 1 if key == "ocr_pages" else 1000, "credits_per_unit": 4, "enabled": True}
            for key in TEMPLATE_CAPABILITIES}
        db.add(ModelBillingSettings(id=1, default_provider_id=model.id, enabled=False,
            shared_pricing_enabled=True, version=1, template=template))
        db.commit()
        cookie = client.cookies.get(auth.COOKIE)
        model_id = str(model.id)
    @main.app.get("/__fixture/session")
    def fixture_session():
        return {"cookie": cookie, "provider_id": model_id}
    uvicorn.run(main.app, host="127.0.0.1", port=8127, log_level="warning")
