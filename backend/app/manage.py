"""Server-console-only commands. Never expose bootstrap tokens through public APIs."""
import secrets
import sys
import json
from datetime import timedelta
from urllib.parse import quote

from sqlalchemy import delete, select

from .identity.auth import digest, now
from .config import get_settings
from .db import SessionLocal
from .models import AuthToken, User, SiteAuthAudit


def main():
    if len(sys.argv) >= 2 and sys.argv[1] == "personal-accounts":
        from .personal_accounts import cleanup_external_resources, cleanup_suspended_accounts
        if sys.argv[2:] == ["cleanup-suspended"]:
            from .storage import list_object_keys
            with SessionLocal() as db:
                while True:
                    result = cleanup_suspended_accounts(db, list_objects=list_object_keys,
                                                        cleanup_external=cleanup_external_resources)
                    print(json.dumps(result, ensure_ascii=True, indent=2, default=str))
                    if result["accounts_deleted"] == 0 and (
                        not result["objects_deleted"] or not result["remaining_objects"]
                    ):
                        break
                if result["remaining_objects"]:
                    raise SystemExit(1)
            return
        raise SystemExit(
            "Usage: python -m app.manage personal-accounts "
            "cleanup-suspended"
        )
    if len(sys.argv) >= 3 and sys.argv[1] == "pricing":
        from .billing_admin import migration_command
        from fastapi import HTTPException
        if sys.argv[2] not in ("preview", "pause", "resume", "activate") or len(sys.argv) != (4 if sys.argv[2] == "activate" else 3):
            raise SystemExit("Usage: pricing preview|pause|resume|activate <reviewed-fingerprint>")
        with SessionLocal() as db:
            try:
                result = migration_command(db, sys.argv[2], sys.argv[3] if len(sys.argv) == 4 else None)
                db.commit()
            except (ValueError, HTTPException) as exc:
                raise SystemExit(getattr(exc, "detail", str(exc))) from exc
            print(json.dumps(result, ensure_ascii=True, indent=2))
        return
    if sys.argv[1:] == ["models", "migrate"]:
        from .shared_providers import migrate_connections
        with SessionLocal() as db:
            count = migrate_connections(db, get_settings())
            db.commit()
            print(f"Migrated {count} model associations; credentials were not printed.")
        return
    if len(sys.argv) == 3 and sys.argv[1] == "model-billing" and sys.argv[2] in ("status", "enable", "disable"):
        from .model_pricing import switch_billing
        from fastapi import HTTPException
        with SessionLocal() as db:
            try:
                status = switch_billing(db, sys.argv[2])
                db.commit()
            except (ValueError, HTTPException) as exc:
                raise SystemExit(getattr(exc, "detail", str(exc))) from exc
            print(f"Model request billing: {'enabled' if status['billing_enabled'] else 'disabled'}; "
                f"template version: {status['version']}; complete: {status['configured']}")
        return
    if sys.argv[1:] == ["site-audit", "prune"]:
        from .site_audit import prune
        with SessionLocal() as db:
            count = prune(db)
            db.commit()
            print(f"Pruned {count} expired site audit events.")
        return
    if len(sys.argv) == 5 and sys.argv[1] == "site-admin" and sys.argv[2] in ("grant", "revoke") and sys.argv[3] == "--email":
        with SessionLocal() as db:
            user = db.scalar(select(User).where(User.email == sys.argv[4].strip().lower()).with_for_update())
            if user is None:
                raise SystemExit("User not found; create the account before granting site administration.")
            user.is_site_admin = sys.argv[2] == "grant"
            db.add(SiteAuthAudit(actor_user_id=user.id, action=f"auth.site_admin_{sys.argv[2]}", details={"source": "server_console"}))
            from .site_audit import record
            record(db, user, f"auth.site_admin_{sys.argv[2]}", "user", user.id, details={"source": "server_console"})
            db.commit()
            print(f"Site administration {sys.argv[2]} completed.")
        return
    if sys.argv[1:] != ["bootstrap"]:
        raise SystemExit(
            "Usage: python -m app.manage bootstrap | "
            "personal-accounts cleanup-suspended | "
            "site-admin grant|revoke --email <email> | "
            "model-billing status|enable|disable"
        )
    with SessionLocal() as db:
        if db.scalar(select(User.id).limit(1)) is not None:
            raise SystemExit("Initial account setup is already complete.")
        token = secrets.token_urlsafe(48)
        db.execute(delete(AuthToken).where(AuthToken.purpose == "bootstrap"))
        db.add(AuthToken(token_hash=digest(token), purpose="bootstrap", expires_at=now()+timedelta(hours=1)))
        db.commit()
        print("One-time setup token (valid for 1 hour):")
        print(token)
        print("One-time setup link:")
        print(f"{get_settings().frontend_url.rstrip('/')}/?setup={quote(token)}")


if __name__ == "__main__":
    main()
