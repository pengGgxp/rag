"""Ordinary file execution, failure recovery and local runner safety."""
import base64
import io
import os
import subprocess
import tarfile
from contextlib import nullcontext
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException

from app import extensions, executor_service, workspace_files
from app.config import get_settings
from app.extensions import ExtensionRuntime
from app.models import (AgentSkill, AgentSkillFile, ChatMessage, Conversation, SiteSkill,
    User, WorkspaceExecutionJob, WorkspaceFile, WorkspaceFileVersion)
from test_extensions import access_for
from test_shared_providers import setup_site


def enable_cloud(db, access, monkeypatch):
    from test_cloud_computer import enable_execution
    return enable_execution(db, access, monkeypatch)


def test_output_tools_require_cloud_and_recheck_suspension(accounts, monkeypatch):
    admin, db, _ = setup_site(accounts, monkeypatch)
    monkeypatch.setattr(get_settings(), "workspace_executor_url", "")
    stored = {}
    monkeypatch.setattr(workspace_files, "upload_fileobj", lambda stream, key, *_: stored.update({key: stream.read()}))
    monkeypatch.setattr(workspace_files, "download_fileobj", lambda key: io.BytesIO(stored[key]))
    monkeypatch.setattr("app.resource_cache.download_range_fileobj", lambda key, start, end: io.BytesIO(stored[key][start:end+1]))
    access = access_for(admin, db)
    runtime = ExtensionRuntime(lambda: nullcontext(db), access, [], request_id=uuid4())
    assert "write_file" not in {item["function"]["name"] for item in runtime.tools}
    assert runtime.execute("list_files", {})["error_code"] == "file_tool_unavailable"
    assert not stored
    assert runtime.execute("run_command", {"command": "echo no"})["error_code"] == "file_tool_unavailable"
    enable_cloud(db, access, monkeypatch)
    runtime = ExtensionRuntime(lambda: nullcontext(db), access, [], request_id=uuid4())
    assert "write_file" in {item["function"]["name"] for item in runtime.tools}
    db.get(User, access.user_id).is_suspended = True
    db.commit()
    with pytest.raises(HTTPException) as error:
        runtime.execute("write_file", {"computer_id": "00000000-0000-4000-a000-000000000112",
            "path": "second.txt", "content": "forbidden"}, call_id="tool-1-forbidden")
    assert error.value.status_code == 403 and not stored


def test_script_nonzero_exit_is_failure_and_keeps_completed_outputs(accounts, monkeypatch):
    admin, db, _ = setup_site(accounts, monkeypatch)
    access = access_for(admin, db)
    fake, execute = enable_cloud(db, access, monkeypatch)
    monkeypatch.setattr(workspace_files, "upload_fileobj", lambda *args: None)
    fake.result = {"exit_code": 1, "stdout": "", "stderr": "synthetic error"}
    fake.outputs = {"partial.txt": b"partial"}
    result = execute({"command": "exit 1"})
    assert result["error_code"] == "script_failed" and not result.get("generated_files")
    assert admin.get("/resources").json()["items"][0]["path"] == "partial.txt"
    from app.models import CloudComputerCommand
    assert db.query(CloudComputerCommand).one().status == "failed"


def test_command_never_scans_reads_or_archives_outputs(accounts, monkeypatch):
    admin, db, _ = setup_site(accounts, monkeypatch)
    access = access_for(admin, db)
    fake, execute = enable_cloud(db, access, monkeypatch)
    fake.outputs = {"report.txt": b"result"}
    def unavailable(*args):
        raise HTTPException(503, "storage unavailable")
    monkeypatch.setattr(workspace_files, "upload_fileobj", unavailable)
    monkeypatch.setattr(fake, "manifest", lambda *_: pytest.fail("post-command scan"))
    monkeypatch.setattr(fake, "read", lambda *_: pytest.fail("output body read"))
    result = execute({"command": "echo result"})
    assert result["status"] == "completed" and not result.get("generated_files")
    assert db.query(WorkspaceFile).count() == db.query(WorkspaceFileVersion).count() == 0
    assert admin.get("/resources").json()["items"][0]["path"] == "report.txt"
    assert fake.starts == 1
    row = db.query(WorkspaceFile).one()
    from app.shared_resources import backend
    monkeypatch.setattr(backend(db), "put", unavailable)
    with pytest.raises(HTTPException):
        workspace_files._save_bytes(access.user_id, access, db, row.path, b"changed", expected_version=1)
    db.commit()
    db.refresh(row)
    assert row.current_version == 1 and row.size_bytes == len(b"result")
    assert db.query(WorkspaceFileVersion).count() == 1


@pytest.mark.parametrize("path", ["../x", "/x", "a/../../x", "a\\x", "a:b", "a//x"])
def test_executor_rejects_unsafe_resource_paths(path):
    with pytest.raises(HTTPException):
        executor_service.input_archive({path: "eA=="})


def test_executor_preserves_hidden_names_and_outputs_larger_than_five_mb(monkeypatch):
    data = io.BytesIO()
    payload = b"x" * 6_000_000
    with tarfile.open(fileobj=data, mode="w") as bundle:
        for path, body in [("./large.bin", payload), ("./.result.txt", b"hidden")]:
            info = tarfile.TarInfo(path); info.size = len(body)
            bundle.addfile(info, io.BytesIO(body))
    def process(args, **kwargs):
        if "tar" in args and "-c" in args:
            return subprocess.CompletedProcess(args, 0, data.getvalue(), b"")
        return subprocess.CompletedProcess(args, 0, "" if kwargs.get("text") else b"", "" if kwargs.get("text") else b"")
    monkeypatch.setattr(executor_service.subprocess, "run", process)
    result = executor_service.run(executor_service.RunRequest(account_id="test", command="true"))
    assert base64.b64decode(result["files"]["large.bin"]) == payload
    assert base64.b64decode(result["files"][".result.txt"]) == b"hidden"


def test_network_setup_failure_never_starts_untrusted_command(monkeypatch):
    calls = []
    def process(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 1 if "--cap-add=NET_ADMIN" in args else 0, b"", b"")
    monkeypatch.setattr(executor_service.subprocess, "run", process)
    with pytest.raises(HTTPException) as error:
        executor_service.run(executor_service.RunRequest(account_id="test", command="echo untrusted"))
    assert error.value.status_code == 503
    assert not any("--name" in args for args in calls)


@pytest.mark.skipif(os.environ.get("RAG_TEST_REAL_EXECUTOR") != "1", reason="requires Docker and public network")
def test_real_runner_allows_public_https_and_blocks_private_metadata():
    code = """import socket, urllib.request
assert urllib.request.urlopen('https://example.com', timeout=15).status == 200
for host in ('169.254.169.254', '10.0.0.1', '172.17.0.1'):
    try:
        connection = socket.create_connection((host, 80), timeout=2)
    except OSError:
        continue
    connection.close()
    raise AssertionError('private destination reachable')
print('PUBLIC_OK PRIVATE_BLOCKED')
"""
    result = executor_service.run(executor_service.RunRequest(account_id="test", command="python test/network.py",
        files={"test/network.py": base64.b64encode(code.encode()).decode()}))
    assert result["exit_code"] == 0 and "PUBLIC_OK PRIVATE_BLOCKED" in result["stdout"], result
