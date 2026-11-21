"""Append-only, allowlisted security events; no request payloads or credentials."""
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete

from .models import SiteAuditEvent, SiteAuthAudit

RETENTION_DAYS = 365
SAFE_FIELDS = {
    "method", "source", "accepted", "fields", "count",
    "sessions_revoked", "previous_suspended", "suspended", "reason_code",
    "account_id", "resource_id",
    "previous_percent", "multiplier_percent", "billing_multiplier_percent", "version", "fingerprint",
    "previous_signup_credits", "signup_credits",
    "request_id", "option_id", "received_thinking", "reasoning_tokens",
    "amount", "balance_before", "balance_after",
}


def record(db, actor, action, target_type, target_id=None, *, request=None, result="success", details=None):
    safe = {key: value for key, value in (details or {}).items() if key in SAFE_FIELDS}
    db.add(SiteAuditEvent(
        actor_user_id=actor.id if actor else None, action=action, target_type=target_type,
        target_id=str(target_id) if target_id is not None else None, result=result, details=safe,
        ip_address=request.client.host[:64] if request and request.client else None,
    ))


def prune(db):
    cutoff = datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)
    count = db.execute(delete(SiteAuditEvent).where(SiteAuditEvent.created_at < cutoff).execution_options(synchronize_session="fetch")).rowcount
    db.execute(delete(SiteAuthAudit).where(SiteAuthAudit.created_at < cutoff).execution_options(synchronize_session="fetch"))
    return count
