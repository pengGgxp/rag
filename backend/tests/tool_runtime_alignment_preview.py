"""Disposable real API fixture for Issue #111 authorization and settings acceptance."""
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
import sys
import tempfile

from cryptography.fernet import Fernet

root = Path(tempfile.mkdtemp(prefix="rag-issue111-"))
os.environ["FRONTEND_URL"] = "http://localhost:3111"
os.environ["API_PUBLIC_URL"] = "http://localhost:18111"
os.environ["DATABASE_URL"] = f"sqlite:///{root / 'preview.db'}"
os.environ["SITE_CONFIG_ENCRYPTION_KEY"] = Fernet.generate_key().decode()
os.environ["MCP_CREDENTIAL_KEY"] = Fernet.generate_key().decode()
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient
from app import auth, main, mcp_client
from app.db import Base, engine, SessionLocal
from app.models import MCPServer, LEGACY_WORKSPACE_ID
from auth_helpers import authenticate
import uvicorn


def definitions():
    return [{"name": f"read_document_{index:03d}",
        "description": f"查阅第 {index} 组资料并返回原文片段。" + ("长说明用于检查窄屏换行。" * 8 if index == 0 else ""),
        "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}},
        "annotations": {"readOnlyHint": True}} for index in range(150)]


if __name__ == "__main__":
    Base.metadata.create_all(engine)
    mcp_client.discover = lambda *_: definitions()
    @asynccontextmanager
    async def lifespan(_app):
        yield
    main.app.router.lifespan_context = lifespan
    with SessionLocal() as db:
        http = TestClient(main.app)
        user = authenticate(http, db)
        user.email_verified_at, user.is_site_admin = auth.now(), True
        server = MCPServer(workspace_id=LEGACY_WORKSPACE_ID, owner_id=user.id,
            name="资料查询验收服务", url="https://fixture.example/mcp", transport="streamable_http",
            version=1, authorization_version=1, enabled=False, tools=definitions(), allowed_tools=[],
            last_checked_at=datetime.now(timezone.utc), header_names=[])
        db.add(server); db.commit()
        cookie, server_id = http.cookies.get(auth.COOKIE), str(server.id)

    @main.app.get("/__fixture/session")
    def fixture_session():
        return {"cookie": cookie, "server_id": server_id}

    print(f"Isolated browser fixture: {root}", flush=True)
    uvicorn.run(main.app, host="127.0.0.1", port=18111, log_level="warning")
