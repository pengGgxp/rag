from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from threading import Barrier
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import model_billing, search_billing
from app.models import CreditAccount, CreditLedgerEntry, SearchToolCall, SiteSearchSettings, UsageEvent
from app.web_search import SearchConfig
from test_auth_postgres import auth_postgres
from test_model_billing_postgres import setup


def test_concurrent_tool_claim_and_settlement_are_unique(auth_postgres):
    engine = auth_postgres
    workspace, user = setup(engine)
    request = uuid4()
    prices = {key: {"capability": key, "unit": "calls", "unit_size": 1000,
        "credits_per_unit": 5, "enabled": True} for key in ("web_search", "web_fetch")}
    with Session(engine) as db:
        settings = db.get(SiteSearchSettings, 1)
        if not settings:
            settings = SiteSearchSettings(id=1)
            db.add(settings)
        settings.enabled = True
        model_billing.start_request(db, workspace, user, request, None, 0,
            search_config=SearchConfig(version=1, api_key="test", prices=prices), tool_only=True)
        db.commit()
    barrier = Barrier(4)
    def claim(_):
        with Session(engine) as db:
            barrier.wait(timeout=10)
            row, fresh = search_billing.begin(db, request, "web_search", "same-key")
            db.commit()
            return row.id, fresh
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(claim, range(4)))
    assert len({item[0] for item in results}) == 1
    assert sum(item[1] for item in results) == 1
    with Session(engine) as db:
        row = db.get(SearchToolCall, results[0][0])
        row.status, row.units = "completed", 1
        db.commit()
    barrier = Barrier(4)
    def settle(_):
        with Session(engine) as db:
            barrier.wait(timeout=10)
            value = model_billing.finalize_request(db, request, "cancelled")
            db.commit()
            return value
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert list(pool.map(settle, range(4))) == [Decimal("0.005")] * 4
    with Session(engine) as db:
        assert db.get(CreditAccount, workspace).balance == Decimal("999.995")
        assert db.scalar(select(func.count()).select_from(UsageEvent).where(UsageEvent.request_id == request)) == 1
        assert db.scalar(select(func.count()).select_from(CreditLedgerEntry).where(
            CreditLedgerEntry.workspace_id == workspace, CreditLedgerEntry.kind == "usage_settlement")) == 1
