"""Real admin HTTP acceptance on isolated workspaces; no real member balances changed."""
import json
from uuid import uuid4

import httpx
from sqlalchemy import delete, func, select

from app.identity import auth
from app.infra.config import get_settings
from app.infra.db import SessionLocal
from app.models import CreditLedgerEntry, LoginSession, ModelBillingSettings, SiteAuditEvent, User


def main():
    created = []
    settings = get_settings()
    try:
        with SessionLocal() as db:
            for name in ("Administrator", "Member"):
                user, workspace = auth.create_user(db, f"issue53-{uuid4().hex}@example.com", f"Issue53 {name}")
                user.email_verified_at = auth.now()
                user.is_site_admin = name == "Administrator"
                response = auth.login_response(db, user, workspace_id=workspace)
                created.append((user.id, workspace, response))
            billing = db.get(ModelBillingSettings, 1)
            before = (billing.enabled, billing.shared_pricing_enabled, str(billing.default_provider_id))
        def client(response):
            token = response.headers["set-cookie"].split(";", 1)[0].split("=", 1)[1]
            return httpx.Client(base_url=settings.api_public_url.rstrip("/"), timeout=30,
                cookies={auth.COOKIE: token}, headers={"X-RAG-Request": "1",
                    "X-CSRF-Token": json.loads(response.body)["csrf_token"], "Origin": settings.frontend_url})
        with client(created[0][2]) as admin, client(created[1][2]) as member:
            workspace = str(created[1][1])
            path = f"/site/admin/workspaces/{workspace}/credits"
            summary = admin.get(path).json()
            initial = summary["credits"]["balance"]
            admin_balance = admin.get("/usage").json()["balance"]
            payload = {"request_id": str(uuid4()), "direction": "add", "amount": 23,
                "expected_balance": initial, "reason": "Issue53 isolated acceptance"}
            assert member.get(path).status_code == 403
            assert member.post(path + "/adjustments", json=payload).status_code == 403
            first = admin.post(path + "/adjustments", json=payload)
            assert first.status_code == 200, first.status_code
            assert admin.post(path + "/adjustments", json=payload).json() == first.json()
            usage = member.get("/usage")
            assert usage.json()["balance"] == initial + 23
            assert "Issue53 isolated acceptance" not in usage.text
            assert admin.post(path + "/adjustments", json={**payload, "amount": 24}).status_code == 409
            debit = {**payload, "request_id": str(uuid4()), "direction": "deduct", "expected_balance": initial + 23}
            assert admin.post(path + "/adjustments", json=debit).status_code == 200
            assert member.get("/usage").json()["balance"] == initial
            assert admin.post(path + "/adjustments", json={**debit, "request_id": str(uuid4()),
                "expected_balance": initial, "amount": initial + 1}).status_code == 422
            assert admin.get("/usage").json()["balance"] == admin_balance
            assert admin.get("/auth/me").json()["workspace_id"] == str(created[0][1])
            with SessionLocal() as db:
                assert db.scalar(select(func.count()).select_from(CreditLedgerEntry).where(
                    CreditLedgerEntry.workspace_id == created[1][1],
                    CreditLedgerEntry.kind.in_(("manual_credit", "manual_debit")))) == 2
                assert db.scalar(select(func.count()).select_from(SiteAuditEvent).where(
                    SiteAuditEvent.target_id == workspace, SiteAuditEvent.action == "workspace.credits_adjusted")) == 2
                billing = db.get(ModelBillingSettings, 1)
                assert before == (billing.enabled, billing.shared_pricing_enabled, str(billing.default_provider_id))
            print(json.dumps({"passed": True, "target_workspace": workspace, "adjustments": 2,
                "net_balance_change": 0, "member_write_denied": True, "duplicate_applied_once": True}))
    finally:
        with SessionLocal() as db:
            for user_id, _, _ in created:
                user = db.get(User, user_id)
                user.is_site_admin, user.is_suspended = False, True
                db.execute(delete(LoginSession).where(LoginSession.user_id == user_id))
            db.commit()
        print("Isolated acceptance accounts suspended; temporary sessions revoked.")


if __name__ == "__main__":
    main()
