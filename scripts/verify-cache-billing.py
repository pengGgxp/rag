"""Bounded real HTTP/SSE acceptance; retain the isolated workspace's accounting records."""
import argparse
from fractions import Fraction
import json
from pathlib import Path
import runpy
from uuid import uuid4

import httpx
from httpx_sse import EventSource
from sqlalchemy import delete, select

from app import auth
from app.config import get_settings
from app.credit_amounts import amount
from app.db import SessionLocal
from app.models import (CreditAccount, CreditLedgerEntry, LoginSession, ModelBillingSettings,
                        ModelCallUsage, ModelUsageRequest, UsageEvent, User)


def live(model_name):
    configuration = runpy.run_path(str(Path(__file__).with_name("verify-decimal-billing.py")))["configuration"]
    settings = get_settings()
    user_id, baseline = None, None
    results = []
    try:
        with SessionLocal() as db:
            assert db.get(ModelBillingSettings, 1).enabled, "Cache settlement is not enabled"
            user, workspace = auth.create_user(db, f"cache-{uuid4().hex}@example.com", "计费验收")
            user_id = user.id
            user.email_verified_at = auth.now()
            response = auth.login_response(db, user, workspace_id=workspace)
            token = response.headers["set-cookie"].split(";", 1)[0].split("=", 1)[1]
            baseline = configuration(db)
        # Identical synthetic prefixes, separate chats, at most three billable requests.
        query = "Read this synthetic test fixture; use no tools and reply only OK.\n" + "\n".join(
            f"row-{i:04d}: alpha bravo charlie delta echo foxtrot golf hotel india juliet."
            for i in range(180))
        with httpx.Client(base_url=settings.api_public_url.rstrip("/"), timeout=180,
            cookies={auth.COOKIE: token}, headers={"X-RAG-Request": "1", "Origin": settings.frontend_url,
                "X-CSRF-Token": json.loads(response.body)["csrf_token"]}) as client:
            choices = client.get("/models/available").json()
            choice = next(row for row in choices["providers"] if row["model"] == model_name and row["available"])
            for _ in range(3):
                with SessionLocal() as db:
                    balance = db.get(CreditAccount, workspace).balance
                created = client.post("/conversations", json={})
                assert created.status_code == 201
                path = f"/conversations/{created.json()['id']}/messages"
                request_id = uuid4()
                payload = {"provider_id": choice["id"], "request_id": str(request_id), "query": query}
                terminal, deltas = None, 0
                with client.stream("POST", path, json=payload) as stream:
                    assert stream.status_code == 200, f"Request HTTP {stream.status_code}"
                    for event in EventSource(stream).iter_sse():
                        if event.event == "delta":
                            deltas += 1
                        if event.event == "done":
                            terminal = event.json()
                assert terminal and terminal["status"] == "completed", "Question did not complete"
                with SessionLocal() as db:
                    calls = db.scalars(select(ModelCallUsage).where(ModelCallUsage.request_id == request_id)).all()
                    events = db.scalars(select(UsageEvent).where(UsageEvent.request_id == request_id)).all()
                    assert calls and len(events) == 3 * len(calls)
                    assert all(row.usage["source"] == "provider" for row in calls), "Estimated usage is not live acceptance"
                    for call in calls:
                        usage = call.usage
                        assert usage["input_tokens"] + (usage["cached_input_tokens"] or 0) == usage["prompt_tokens"]
                    exact = sum((Fraction(row.units, row.policy_snapshot["unit_size"])
                        * Fraction(str(row.policy_snapshot["credits_per_unit"]))
                        * Fraction(row.policy_snapshot["multiplier_percent"], 100) for row in events), Fraction())
                    billed = sum(row.credits for row in events)
                    assert billed == amount(exact)
                    account = db.get(CreditAccount, workspace)
                    assert account.balance == balance - billed
                    request = db.get(ModelUsageRequest, request_id)
                    assert request.status == "settled" and request.credits == billed
                    charges = db.scalars(select(CreditLedgerEntry).where(
                        CreditLedgerEntry.workspace_id == workspace,
                        CreditLedgerEntry.idempotency_key == f"model:{request_id}:charge")).all()
                    assert len(charges) == (1 if billed else 0)
                    saved_balance, event_ids = account.balance, {row.id for row in events}
                    summary = {"request_id": str(request_id), "model": model_name, "deltas": deltas,
                        "input_total": sum(row.usage["prompt_tokens"] for row in calls),
                        "ordinary_input": sum(row.usage["input_tokens"] for row in calls),
                        "cache_read": sum(row.usage["cached_input_tokens"] or 0 for row in calls),
                        "output": sum(row.usage["completion_tokens"] for row in calls),
                        "credits": str(billed)}
                replay = client.post(path, json=payload)
                assert replay.status_code in (200, 409)
                with SessionLocal() as db:
                    assert db.get(CreditAccount, workspace).balance == saved_balance
                    assert {row.id for row in db.scalars(select(UsageEvent).where(
                        UsageEvent.request_id == request_id))} == event_ids
                    assert configuration(db) == baseline, "Commercial configuration changed"
                results.append(summary)
                print(json.dumps(summary), flush=True)
                if summary["cache_read"] > 0:
                    print("PASS: real cache hit, three price categories, four decimals, one debit, replay unchanged.")
                    return
            raise AssertionError("No real cache hit observed within three calls; acceptance remains incomplete")
    finally:
        if user_id:
            with SessionLocal() as db:
                db.get(User, user_id).is_suspended = True
                db.execute(delete(LoginSession).where(LoginSession.user_id == user_id))
                if baseline is not None:
                    assert configuration(db) == baseline
                db.commit()
            print("Isolated account suspended; sessions revoked; billing records retained.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True, help="Exact configured model ID")
    parser.add_argument("--run-live", action="store_true")
    args = parser.parse_args()
    if not args.run_live:
        parser.error("Real paid requests require --run-live")
    live(args.model)
