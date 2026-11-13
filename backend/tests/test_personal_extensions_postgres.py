"""Concurrent personal edits use the same locking boundary as permission checks."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import UUID

from sqlalchemy.orm import Session
from app.models import MCPServer
from test_auth_postgres import auth_postgres
from test_platform_skills_postgres import actor, call


def test_mcp_versions_are_atomic(auth_postgres):
    raw, csrf, _ = actor(auth_postgres)
    mcp = {"name": "Remote", "url": "https://mcp.example.com/mcp"}
    remote = call(raw, csrf, "POST", "/agent/mcp", mcp)
    assert remote.status_code == 201, remote.text
    for path, payload in (
        (f"/agent/mcp/{remote.json()['id']}", {**mcp, "version": 1}),
    ):
        barrier = Barrier(2)
        def update(_):
            barrier.wait(timeout=10)
            return call(raw, csrf, "PUT", path, payload).status_code
        with ThreadPoolExecutor(2) as pool:
            assert sorted(pool.map(update, range(2))) == [200, 409]
    with Session(auth_postgres) as db:
        assert db.get(MCPServer, UUID(remote.json()["id"])).version == 2
