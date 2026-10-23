"""Server-console-only commands. Never expose bootstrap tokens through public APIs."""
import secrets
import sys
from datetime import timedelta
from urllib.parse import quote

from sqlalchemy import delete, select

from .auth import digest, now
from .config import get_settings
from .db import SessionLocal
from .models import AuthToken, Membership, LEGACY_WORKSPACE_ID


def main():
    if sys.argv[1:] != ["bootstrap"]:
        raise SystemExit("Usage: python -m app.manage bootstrap")
    with SessionLocal() as db:
        if db.scalar(select(Membership).where(Membership.workspace_id == LEGACY_WORKSPACE_ID)):
            raise SystemExit("The existing workspace has already been claimed.")
        token = secrets.token_urlsafe(48)
        db.execute(delete(AuthToken).where(AuthToken.purpose == "bootstrap"))
        db.add(AuthToken(token_hash=digest(token), purpose="bootstrap", expires_at=now()+timedelta(hours=1)))
        db.commit()
        print("One-time workspace setup token (valid for 1 hour):")
        print(token)
        print("One-time workspace setup link:")
        print(f"{get_settings().frontend_url.rstrip('/')}/?setup={quote(token)}")


if __name__ == "__main__":
    main()
