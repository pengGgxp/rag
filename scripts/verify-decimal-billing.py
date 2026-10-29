"""Read-only preservation checks and one explicitly enabled isolated live question."""
import argparse
from decimal import Decimal
from fractions import Fraction
import hashlib
import json
from pathlib import Path
from uuid import uuid4

import httpx
from httpx_sse import EventSource
from sqlalchemy import delete, select

from app import auth
from app.config import get_settings
from app.db import SessionLocal
from app.models import (CreditAccount, CreditLedgerEntry, LoginSession, ModelBillingSettings,
                        ModelConnection, ModelUsageRequest, SiteModelProvider, UsageEvent, UsagePolicy, User, Workspace)


def decimal(value):
    return format(Decimal(str(value)).normalize(), "f")


def configuration(db):
    settings = db.get(ModelBillingSettings, 1)
    state = {
        "settings": [settings.enabled, settings.shared_pricing_enabled, settings.pricing_paused,
            str(settings.default_provider_id), settings.template, settings.version],
        "providers": [(str(row.id), row.version, row.prices, row.enabled)
            for row in db.scalars(select(SiteModelProvider).order_by(SiteModelProvider.id))],
        "connections": [(str(row.id), row.version, row.enabled)
            for row in db.scalars(select(ModelConnection).order_by(ModelConnection.id))],
        "multipliers": [(str(row.id), row.billing_multiplier_percent, row.billing_version)
            for row in db.scalars(select(Workspace).order_by(Workspace.id))],
        "legacy_prices": [(str(row.id), decimal(row.credits_per_unit), row.unit_size, row.version)
            for row in db.scalars(select(UsagePolicy).order_by(UsagePolicy.id))],
    }
    return hashlib.sha256(json.dumps(state, sort_keys=True).encode()).hexdigest()


def snapshot(db):
    return {"configuration": configuration(db), "tables": {
        "accounts": {str(row.workspace_id): [decimal(row.balance), decimal(row.lifetime_granted), decimal(row.lifetime_spent)]
            for row in db.scalars(select(CreditAccount))},
        "ledger": {str(row.id): [decimal(row.amount), decimal(row.balance_after)]
            for row in db.scalars(select(CreditLedgerEntry))},
        "usage": {str(row.id): decimal(row.credits) for row in db.scalars(select(UsageEvent))},
        "requests": {str(row.request_id): [decimal(row.credits), decimal(row.hold), row.status]
            for row in db.scalars(select(ModelUsageRequest))},
    }}


def live():
    from app.credit_amounts import amount
    settings = get_settings()
    identifier = None
    before = None
    try:
        with SessionLocal() as db:
            user, workspace = auth.create_user(db, f"issue57-{uuid4().hex}@example.com", "积分验收")
            identifier = user.id
            user.email_verified_at = auth.now()
            response = auth.login_response(db, user, workspace_id=workspace)
            token = response.headers["set-cookie"].split(";", 1)[0].split("=", 1)[1]
            balance = db.get(CreditAccount, workspace).balance
            before = configuration(db)
        with httpx.Client(base_url=settings.api_public_url.rstrip("/"), timeout=120,
            cookies={auth.COOKIE: token}, headers={"X-RAG-Request": "1",
                "Origin": settings.frontend_url, "X-CSRF-Token": json.loads(response.body)["csrf_token"]}) as client:
            choices = client.get("/models/available").json()
            choice = next((row for row in choices["providers"] if row["available"]
                and row["id"] == choices["default_provider_id"]),
                next(row for row in choices["providers"] if row["available"]))
            conversation = client.post("/conversations", json={})
            assert conversation.status_code == 201
            path = f"/conversations/{conversation.json()['id']}/messages"
            request = uuid4()
            payload = {"provider_id": choice["id"], "request_id": str(request), "query": "你好"}
            terminal, deltas = None, 0
            with client.stream("POST", path, json=payload) as upstream:
                assert upstream.status_code == 200
                for event in EventSource(upstream).iter_sse():
                    if event.event == "delta":
                        deltas += 1
                    if event.event == "done":
                        terminal = event.json()
            assert terminal and terminal["status"] == "completed", "Live question did not complete"
            with SessionLocal() as db:
                events = db.scalars(select(UsageEvent).where(UsageEvent.request_id == request)).all()
                assert events and all(row.metadata_json.get("source") == "provider" for row in events)
                exact = sum((Fraction(row.units, row.policy_snapshot["unit_size"])
                    * Fraction(str(row.policy_snapshot["credits_per_unit"]))
                    * Fraction(row.policy_snapshot.get("multiplier_percent", 100), 100)
                    for row in events), Fraction())
                billed = sum(row.credits for row in events)
                assert billed == amount(exact)
                account = db.get(CreditAccount, workspace)
                assert account.balance == balance - billed, "One request must debit exactly once"
                assert not db.get(ModelBillingSettings, 1).enabled, "Issue #36 must stay disabled"
                assert not db.scalars(select(ModelUsageRequest).where(
                    ModelUsageRequest.workspace_id == workspace, ModelUsageRequest.status == "pending")).all()
                saved_balance = account.balance
            replay = client.post(path, json=payload)
            assert replay.status_code == 409, f"Expected replay rejection, received {replay.status_code}"
            with SessionLocal() as db:
                assert db.get(CreditAccount, workspace).balance == saved_balance
            print(json.dumps({"passed": True, "model": choice["model"], "deltas": deltas,
                "tokens": {key: sum(row.units for row in events if row.capability == key)
                    for key in {row.capability for row in events}},
                "credits": str(billed), "request_id": str(request), "replay": "no duplicate debit"}))
    finally:
        if identifier:
            with SessionLocal() as db:
                db.get(User, identifier).is_suspended = True
                db.execute(delete(LoginSession).where(LoginSession.user_id == identifier))
                if before is not None:
                    assert configuration(db) == before, "Commercial configuration changed"
                db.commit()
        print("Isolated account suspended; sessions revoked; ledger retained.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=["snapshot", "check", "live"])
    parser.add_argument("--file", type=Path)
    parser.add_argument("--run-live", action="store_true")
    args = parser.parse_args()
    if args.operation == "live":
        if not args.run_live:
            parser.error("Live consumption requires --run-live")
        live()
    else:
        with SessionLocal() as db:
            state = snapshot(db)
        if args.operation == "snapshot":
            print(json.dumps(state, sort_keys=True))
        else:
            assert args.file and state == json.loads(args.file.read_text()), "Stored configuration or historic amounts changed"
            print("Configuration, balances, historical usage and pending amounts preserved.")
