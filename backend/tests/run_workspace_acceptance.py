"""Create a fresh disposable migrated PostgreSQL database for Issue #100."""
import os
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url


def main():
    base = make_url(os.environ["DATABASE_URL"])
    if base.host != "db":
        raise RuntimeError("run inside the isolated Docker project only")
    name = "rag_auth_test_issue100_" + uuid4().hex[:12]
    admin = create_engine(base.set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as db:
        db.execute(text(f'CREATE DATABASE "{name}"'))
    url = base.set(database=name).render_as_string(hide_password=False)
    env = {key: value for key, value in os.environ.items() if key in ("PATH", "LANG", "HOME", "PYTHONUNBUFFERED")}
    env.update(DATABASE_URL=url, AUTH_TEST_DATABASE_URL=url, PYTHONPATH=".:tests", AUTH_COOKIE_SECURE="false")
    root = Path(__file__).resolve().parents[1]
    try:
        for _ in range(2):
            subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=root, env=env, check=True)
        subprocess.run([sys.executable, "-m", "pytest", "tests/test_workspace_mutations_postgres.py", "-q"], cwd=root, env=env, check=True)
        print("ISOLATED_WORKSPACE_POSTGRES_ACCEPTANCE_PASSED")
    finally:
        with admin.connect() as db:
            db.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))
        admin.dispose()


if __name__ == "__main__":
    main()
