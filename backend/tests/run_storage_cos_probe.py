"""Opt-in real COS/HTTP probe using an isolated database and one disposable object."""
import argparse
from datetime import timedelta
import hashlib
import io
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import threading
import time
from uuid import uuid4

import httpx
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session
import uvicorn


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--confirm-live-probe", action="store_true", required=True)
    parser.parse_args()
    from app.infra.config import get_settings
    from app.models import SiteStorageSettings
    from app.infra.cos_storage import CosStorage
    original = make_url(os.environ["DATABASE_URL"])
    production = create_engine(original)
    with Session(production) as db:
        config = db.get(SiteStorageSettings, 1)
        if not config or not config.active:
            raise RuntimeError("COS is not ready")
        storage = CosStorage(config)
    production.dispose()
    name = f"rag_auth_test_storage_cos_{uuid4().hex[:12]}"
    admin = create_engine(original.set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as db:
        db.execute(text(f'CREATE DATABASE "{name}"'))
    url = original.set(database=name).render_as_string(hide_password=False)
    key = f"_checks/storage72-{uuid4()}"
    engine, server, thread = None, None, None
    try:
        os.environ["DATABASE_URL"] = url
        os.environ["AUTH_COOKIE_SECURE"] = "false"
        get_settings.cache_clear()
        subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=Path(__file__).resolve().parents[1], check=True, stdout=subprocess.DEVNULL)
        from app import main as application
        from app.files import downloads as storage_downloads
        from app.identity import auth
        from app.models import UserCreditAccount, LoginSession, User, WorkspaceFile, WorkspaceFileVersion
        from app.storage_models import StorageEntitlement, StorageQuotaSettings, StorageTransfer
        from app.infra.db import get_db
        engine = create_engine(url)
        body = b"storage72-real-transfer\n" * 131072
        storage.put(key, io.BytesIO(body), "application/octet-stream")
        assert int(storage.head(key)["Content-Length"]) == len(body)
        raw, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        with Session(engine) as db:
            user = User(email=f"storage-probe-{uuid4().hex}@example.com", name="Probe")
            db.add(user); db.flush()
            db.add(UserCreditAccount(user_id=user.id, balance=10, lifetime_granted=10, lifetime_spent=0))
            db.add(LoginSession(token_hash=auth.digest(raw), user_id=user.id,
                csrf_token=csrf, expires_at=auth.now()+timedelta(hours=1)))
            file = WorkspaceFile(account_id=user.id, owner_user_id=user.id, path="probe.bin", size_bytes=len(body),
                content_type="application/octet-stream", content_hash=hashlib.sha256(body).hexdigest(),
                current_version=1, created_by=user.id, updated_by=user.id, expires_at=auth.now()+timedelta(days=3))
            db.add(file); db.flush()
            db.add(WorkspaceFileVersion(file_id=file.id, account_id=user.id, version=1, object_key=key,
                size_bytes=len(body), content_type=file.content_type, content_hash=file.content_hash, created_by=user.id))
            db.add(StorageQuotaSettings(id=1, version=1, free_storage_bytes=512*1024**2,
                free_download_bytes=20*1024**2, output_limit_bytes=512*1024**2, retention_days=3,
                packages=[{"id":"probe", "name":"Probe", "storage_bytes":1024, "download_bytes":1024,
                    "days":30, "price_credits":"0.1234", "enabled":True, "order":0}], costs={}))
            db.commit()
            account_id, file_id = user.id, file.id
        def database():
            with Session(engine, autoflush=False) as db:
                yield db
        application.app.dependency_overrides[get_db] = database
        storage_downloads.download_range_fileobj = lambda _, start, end: storage.download_range(key, start, end)
        sock = socket.socket(); sock.bind(("127.0.0.1",0)); port=sock.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(application.app, host="127.0.0.1", port=port,
            log_level="error", lifespan="off"))
        thread = threading.Thread(target=server.run, kwargs={"sockets":[sock]}, daemon=True); thread.start()
        for _ in range(100):
            if server.started: break
            time.sleep(0.05)
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=30,
                cookies={auth.COOKIE:raw}, headers={"X-RAG-Request":"1", "X-CSRF-Token":csrf}) as client:
            path = f"/files/{file_id}/download"
            assert client.head(path).status_code == 200
            response=client.get(path, headers={"Range":"bytes=1000-1999"})
            assert response.status_code == 206 and response.content == body[1000:2000]
            response=client.get(path)
            assert response.status_code == 200 and hashlib.sha256(response.content).digest()==hashlib.sha256(body).digest()
            assert httpx.get(f"http://127.0.0.1:{port}{path}").status_code==401
            purchase={"package_id":"probe", "version":1, "request_id":str(uuid4())}
            first=client.post("/storage/purchases",json=purchase)
            assert first.status_code==201, first.text
            assert client.post("/storage/purchases",json=purchase).json()["id"]==first.json()["id"]
            with client.stream("GET",path) as response:
                assert response.status_code==200
                next(response.iter_bytes(65536))
        time.sleep(0.5)
        with Session(engine) as db:
            rows=db.scalars(select(StorageTransfer).order_by(StorageTransfer.created_at)).all()
            assert len(rows)==3 and rows[0].sent_bytes+rows[1].sent_bytes==len(body)+1000
            assert all(row.status in ("completed","interrupted") for row in rows)
            assert all(row.reserved_bytes==0 for row in db.scalars(select(StorageEntitlement)))
            assert str(db.get(UserCreditAccount,account_id).balance)=="9.8766"
            print(f"REAL_COS_HTTP_PROBE_PASSED bytes={len(body)} range=1000 transfers=3 debit=0.1234 once")
    finally:
        if server: server.should_exit=True
        if thread: thread.join(timeout=10)
        if engine: engine.dispose()
        storage.delete(key)
        assert storage.head(key) is None
        with admin.connect() as db:
            db.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))
        admin.dispose()
        print("ISOLATED_OBJECT_AND_DATABASE_REMOVED")


if __name__ == "__main__":
    main()
