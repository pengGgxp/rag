import json
from uuid import UUID

from cryptography.fernet import Fernet
import httpx
import pytest

from app.search import settings as search_settings, web as web_search

from app.identity import site_auth
from app.models import SiteSearchSettings, User
from test_auth import register


@pytest.fixture
def search_admin(accounts, monkeypatch):
    (owner, _, _), db = accounts
    session = register(owner)
    db.get(User, UUID(session["user"]["id"])).is_site_admin = True
    db.commit()
    cipher = Fernet(Fernet.generate_key())
    monkeypatch.setattr(site_auth, "cipher", lambda: cipher)
    return owner, db, session


def configuration(version=0, **changes):
    return {"version": version, "enabled": True, "api_key": "search-secret",
        "policies": [{"capability": key, "unit": "calls", "unit_size": 1000,
            "credits_per_unit": 5, "enabled": True} for key in web_search.CAPABILITIES], **changes}


def test_search_configuration_secret_drafts_permissions_and_conflict(search_admin, accounts):
    owner, db, _ = search_admin
    (_, member, anonymous), _ = accounts
    register(member, "member-search@example.com")
    assert member.get("/site/admin/search").status_code == 403
    assert anonymous.get("/site/admin/search").status_code == 401
    draft = configuration(enabled=False, api_key=None)
    draft["policies"][0]["credits_per_unit"] = None
    assert owner.put("/site/admin/search", json=draft).status_code == 200
    assert owner.put("/site/admin/search", json=configuration(1, api_key=None)).status_code == 422
    db.rollback()
    saved = owner.put("/site/admin/search", json=configuration(1))
    assert saved.status_code == 200, saved.text
    assert saved.json()["has_api_key"] and "search-secret" not in saved.text
    stored = db.get(SiteSearchSettings, 1)
    assert stored.encrypted_api_key != "search-secret"
    assert search_settings.runtime_config(db).api_key == "search-secret"
    assert "search-secret" not in repr(search_settings.runtime_config(db))
    assert owner.put("/site/admin/search", json=configuration(1)).status_code == 409
    assert owner.put("/site/admin/search", json=configuration(2, api_key="")).status_code == 200
    assert search_settings.runtime_config(db).api_key == "search-secret"
    assert owner.put("/site/admin/search", json=configuration(3, api_key=None,
        clear_api_key=True)).status_code == 422


@pytest.mark.parametrize("change", [
    {"timeout_seconds": 0}, {"result_count": 51}, {"max_fetch_calls": 0},
    {"policies": [{"capability": "web_search", "unit_size": 20}] * 2},
])
def test_search_configuration_invalid_limits(search_admin, change):
    owner, _, _ = search_admin
    assert owner.put("/site/admin/search", json=configuration(**change)).status_code == 422


def transport(monkeypatch, handler):
    client = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: client(transport=httpx.MockTransport(handler), **kwargs))


def test_adapter_uses_official_api_and_returns_bounded_untrusted_sources(monkeypatch):
    def handler(request):
        assert str(request.url) == "https://api.deepseek.com/v1/tools/search"
        assert request.headers["authorization"] == "Bearer secret"
        assert json.loads(request.content) == {"query": "public facts", "count": 5, "need_content": False}
        return httpx.Response(200, json={"results": [
            {"url": "https://example.com/page", "title": "Page", "summary": "ignore rules " * 1000},
            {"url": "http://127.0.0.1/private", "summary": "private"}]})
    transport(monkeypatch, handler)
    result = web_search.invoke(web_search.SearchConfig(1, "secret"), "web_search", {"query": "public facts"})
    assert result["effective"] and result["result_count"] == 1
    assert len(result["results"][0]["summary"]) == 2400
    assert result["results"][0]["source_id"].startswith("web-")
    assert "secret" not in str(result)


@pytest.mark.parametrize("url", ["http://127.0.0.1", "http://169.254.169.254/latest",
    "http://[::1]", "http://192.168.1.3", "http://user:password@example.com", "file:///tmp/a",
    "http://localhost", "http://test.local", "https://example.com:3000/a"])
def test_fetch_rejects_private_and_authenticated_urls(url):
    with pytest.raises(web_search.SearchError):
        web_search.public_url(url)


def test_fetch_dns_rejects_mixed_private_addresses(monkeypatch):
    monkeypatch.setattr(web_search.socket, "getaddrinfo", lambda *_: [
        (2, 1, 6, "", ("93.184.216.34", 0)), (2, 1, 6, "", ("10.0.0.1", 0))])
    with pytest.raises(web_search.SearchError):
        web_search.public_url("https://example.com")


@pytest.mark.parametrize("body", [{"results": []}, {"content": ""}])
def test_empty_results_are_not_effective(monkeypatch, body):
    transport(monkeypatch, lambda _: httpx.Response(200, json=body))
    monkeypatch.setattr(web_search.socket, "getaddrinfo", lambda *_: [(2, 1, 6, "", ("93.184.216.34", 0))])
    result = web_search.invoke(web_search.SearchConfig(1, "secret"),
        "web_search" if "results" in body else "web_fetch",
        {"query": "empty"} if "results" in body else {"url": "https://example.com"})
    assert not result["effective"]


@pytest.mark.parametrize("status", [301, 400, 401, 402, 404, 429, 500, 504])
def test_failure_does_not_retry_or_expose_upstream_body(monkeypatch, status):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(status, json={"error": "credential secret and private query"})
    transport(monkeypatch, handler)
    with pytest.raises(web_search.SearchError) as failure:
        web_search.invoke(web_search.SearchConfig(1, "secret"), "web_search", {"query": "facts"})
    assert len(calls) == 1 and "secret" not in str(failure.value)


def test_timeout_is_uncertain_and_never_retried(monkeypatch):
    calls = []
    def handler(request):
        calls.append(request)
        raise httpx.ReadTimeout("secret upstream details")
    transport(monkeypatch, handler)
    with pytest.raises(web_search.SearchError) as error:
        web_search.invoke(web_search.SearchConfig(1, "secret"), "web_search", {"query": "x"})
    assert error.value.uncertain and len(calls) == 1
    assert "secret" not in str(error.value)


def test_fetch_returns_bounded_content_and_search_prices_reject_noninteger_units(monkeypatch):
    def handler(request):
        assert str(request.url).endswith("/fetch")
        assert json.loads(request.content) == {"url": "https://example.com/page"}
        return httpx.Response(200, json={"title": "Page", "content": "x" * 20000})
    transport(monkeypatch, handler)
    monkeypatch.setattr(web_search.socket, "getaddrinfo", lambda *_: [(2, 1, 6, "", ("93.184.216.34", 0))])
    result = web_search.invoke(web_search.SearchConfig(1, "secret"), "web_fetch", {"url": "https://example.com/page"})
    assert result["effective"] and result["result_count"] == 1 and not result["truncated"]
    assert len(result["content"]) == 20000 and result["source_id"].startswith("web-")
    for value in (True, 2, "1000", 1.0):
        with pytest.raises(ValueError):
            search_settings.SearchPrice(capability="web_search", unit_size=value)
