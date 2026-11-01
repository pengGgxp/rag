"""Explicit isolated PostgreSQL acceptance; credentials stay inside the configured container."""
import os
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url


def main():
    base_url = make_url(os.environ["DATABASE_URL"])
    identifier = f"rag_auth_test_storage_{uuid4().hex[:12]}"
    admin = create_engine(base_url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as db:
        db.execute(text(f'CREATE DATABASE "{identifier}"'))
    url = base_url.set(database=identifier).render_as_string(hide_password=False)
    # Production credentials/cookie policy must not leak into isolated test clients.
    env = {key: value for key, value in os.environ.items() if key in ("PATH", "LANG", "HOME", "PYTHONUNBUFFERED")}
    env.update(DATABASE_URL=url, AUTH_TEST_DATABASE_URL=url, PYTHONPATH=".", AUTH_COOKIE_SECURE="false")
    root = Path(__file__).resolve().parents[1]
    try:
        subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=root, env=env, check=True)
        subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=root, env=env, check=True)
        subprocess.run([sys.executable, "-m", "pytest", "tests/test_storage_packages_postgres.py",
            "tests/test_storage_packages.py", "tests/test_cos_uploads.py", "tests/test_chat_attachments.py",
            "tests/test_skill_script_execution.py", "tests/test_personal_extensions.py",
            "tests/test_pricing_calculator.py", "tests/test_workspace_credits.py", "-q"], cwd=root, env=env, check=True)
        print("ISOLATED_STORAGE_ACCEPTANCE_PASSED")
    finally:
        with admin.connect() as db:
            db.execute(text(f'DROP DATABASE "{identifier}" WITH (FORCE)'))
        admin.dispose()


if __name__ == "__main__":
    main()
