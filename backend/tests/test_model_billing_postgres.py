from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from threading import Barrier
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import model_billing as billing
from app.credits import CreditError, ensure_policies
from app.models import UserCreditAccount, CreditLedgerEntry, ModelCallUsage, ModelUsageRequest, ModelBillingSettings, UsagePolicy, User
from test_auth_postgres import auth_postgres


def setup(engine):
    with Session(engine) as db:
        settings = db.get(ModelBillingSettings, 1)
        if settings:
            settings.shared_pricing_enabled = False
        user = User(email=f"meter-{uuid4().hex}@example.com", name="Meter test")
        db.add(user); db.flush()
        for capability, rate in (("llm_input_tokens", 1), ("llm_output_tokens", 3)):
            db.add(UsagePolicy(account_id=user.id, capability=capability, unit="tokens", unit_size=1000, credits_per_unit=rate))
        db.add(UsagePolicy(account_id=user.id, capability="llm_cached_input_tokens",
            unit="tokens", unit_size=1000, credits_per_unit=0))
        db.commit()
        return user.id, user.id


def test_concurrent_finalize_records_one_charge_and_releases_one_hold(auth_postgres):
    engine = auth_postgres
    workspace, user = setup(engine)
    identifier = uuid4()
    with Session(engine) as db:
        billing.start_request(db, workspace, user, identifier, None, 1)
        model = SimpleNamespace(status=SimpleNamespace(provider_id=None,provider_key=None,model="test"))
        call = billing.begin_call(db, identifier, model)
        billing.finish_call(db, identifier, call, {"input_tokens":1200,"cached_input_tokens":300,
            "completion_tokens":200,"source":"provider"}, "completed")
        db.commit()
    barrier = Barrier(4)
    def finalize(_):
        with Session(engine) as db:
            barrier.wait(timeout=10)
            amount = billing.finalize_request(db, identifier, "completed")
            db.commit()
            return amount
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert list(pool.map(finalize, range(4))) == [Decimal("1.8")]*4
    with Session(engine) as db:
        assert db.get(UserCreditAccount, workspace).balance == Decimal("998.2")
        assert db.scalar(select(func.count()).select_from(CreditLedgerEntry).where(
            CreditLedgerEntry.account_id == workspace, CreditLedgerEntry.kind == "usage_settlement")) == 1


def test_parallel_requests_cannot_overspend_when_paying_pending(auth_postgres):
    engine = auth_postgres
    workspace, user = setup(engine)
    with Session(engine) as db:
        billing.ensure_account(db,workspace)
        db.get(UserCreditAccount, workspace).balance = 101
        db.add(ModelUsageRequest(request_id=uuid4(), account_id=workspace, actor_user_id=user,
            status="pending", credits=100, hold=0))
        db.commit()
    barrier = Barrier(3)
    def request(_):
        with Session(engine) as db:
            barrier.wait(timeout=10)
            try:
                billing.start_request(db,workspace,user,uuid4(),None,1)
                db.commit(); return "ok"
            except CreditError:
                db.rollback(); return "insufficient"
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(request,range(3)))
    assert results.count("ok") == 1
    with Session(engine) as db:
        assert db.get(UserCreditAccount, workspace).balance == 0
        assert billing.pending_credits(db,workspace) == 0
