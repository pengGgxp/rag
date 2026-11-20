from uuid import UUID, uuid4

from sqlalchemy import select

from app.models import ModelConnection, SiteModelProvider
from test_auth import register
from test_shared_providers import setup_site, provider, choose


def test_billing_preview_includes_unused_models_without_writes(accounts, monkeypatch):
    owner, _db, session = setup_site(accounts, monkeypatch)
    response = owner.get(f"/site/admin/workspaces/{session['user']['id']}/billing-settings")
    assert response.status_code == 404


def test_preview_visibility_and_reasons_match_composer(accounts, monkeypatch):
    owner, db, session = setup_site(accounts, monkeypatch)
    path = f"/site/admin/workspaces/{session['user']['id']}/billing-settings"
    assert owner.get(path).status_code == 404
    (_, other, _), _ = accounts
    register(other, "catalog-other@example.com")
    assert other.get(path).status_code == 404


def test_model_detail_permissions_redaction_and_live_diagnostics(accounts, monkeypatch):
    owner, db, _ = setup_site(accounts, monkeypatch)
    model = provider(owner)
    path = f"/site/admin/llm-providers/{model['id']}"
    result = owner.get(path)
    assert result.status_code == 200
    assert result.json()["available"] and result.json()["connection_id"]
    assert "secret" not in result.text and "encrypted_api_key" not in result.text
    db.get(SiteModelProvider, UUID(model["id"])).prices = {}
    db.commit()
    assert owner.get(path).json()["unavailable_reason"] == "pricing_unconfigured"
    assert owner.get("/site/admin/llm-providers").json()[0]["unavailable_reason"] == "pricing_unconfigured"
    assert owner.get(f"/site/admin/llm-providers/{uuid4()}").status_code == 404
    (_, other, anonymous), _ = accounts
    register(other, "detail-member@example.com")
    assert other.get(path).status_code == 403
    assert anonymous.get(path).status_code == 401
