"""Selected Skill packages reach a read-only runner mount, never shared output storage."""
import base64
import io
import os
import subprocess
import tarfile
from contextlib import nullcontext
from types import SimpleNamespace
from uuid import UUID

import pytest
from fastapi import HTTPException

from app import extensions, executor_service, workspace_files
from app.config import get_settings
from app.extensions import ExtensionRuntime
from app.models import AgentSkill, AgentSkillFile, WorkspaceExecutionJob, WorkspaceFile, WorkspaceFileVersion
from test_extensions import access_for
from test_personal_extensions import skill
from test_shared_providers import setup_site


def scripted(client, **changes):
    return skill(client, limitations_acknowledged=True, files=[
        {"path": "scripts/task.py", "content": base64.b64encode(b"print('PINNED SCRIPT')").decode()},
        {"path": "assets/value.bin", "content": base64.b64encode(b"\xff\x00").decode()},
    ], **changes)


def test_runtime_exposes_real_execution_and_passes_only_loaded_pinned_packages(accounts, monkeypatch):
    admin, db, _ = setup_site(accounts, monkeypatch)
    monkeypatch.setattr(get_settings(), "workspace_executor_url", "http://runner.test")
    first, second = scripted(admin), scripted(admin, name="Second")
    runtime = ExtensionRuntime(lambda: nullcontext(db), access_for(admin, db), [first["id"], second["id"]])
    assert "run_command" in {item["function"]["name"] for item in runtime.tools}
    assert runtime.execute("run_command", {"command": "python script.py"}).get("error")
    loaded = runtime.execute("load_skill", {"skill_id": first["id"]})
    assert loaded["execute_scripts"] and loaded["execution"]["directory"] == f"/skills/{first['id']}"
    assert loaded["execution"]["read_only"] and loaded["execution"]["scripts"] == ["scripts/task.py"]
    row = db.get(AgentSkill, UUID(first["id"]))
    row.files[0].content = b"CHANGED AFTER REQUEST"
    row.version += 1
    db.commit()
    captured = []
    monkeypatch.setattr(extensions, "execute_tool", lambda *args, **kwargs: captured.append(kwargs) or {"exit_code": 0})
    assert runtime.execute("run_command", {"command": "python script.py"})["exit_code"] == 0
    package = captured[0]["skill_packages"]
    assert set(package) == {first["id"]} and package[first["id"]]["scripts/task.py"] == b"print('PINNED SCRIPT')"
    assert package[first["id"]]["assets/value.bin"] == b"\xff\x00"
    assert runtime.execute("run_command", {"command": "x", "skill_files": {}}).get("error")
    row.enabled = False
    db.commit()
    assert runtime.execute("run_command", {"command": "python script.py"}).get("error")
    assert len(captured) == 1


def test_missing_executor_does_not_advertise_execution(accounts, monkeypatch):
    admin, db, _ = setup_site(accounts, monkeypatch)
    monkeypatch.setattr(get_settings(), "workspace_executor_url", "")
    row = scripted(admin)
    runtime = ExtensionRuntime(lambda: nullcontext(db), access_for(admin, db), [row["id"]])
    assert "run_command" not in {item["function"]["name"] for item in runtime.tools}
    assert not runtime.execute("load_skill", {"skill_id": row["id"]})["execute_scripts"]
    assert runtime.execute("run_command", {"command": "echo no"}).get("error")


def test_run_command_forwards_packages_and_saves_only_outputs(accounts, monkeypatch):
    admin, db, _ = setup_site(accounts, monkeypatch)
    access = access_for(admin, db)
    monkeypatch.setattr(get_settings(), "workspace_executor_url", "http://runner.test")
    captured, stored = [], {}
    def post(url, **kwargs):
        captured.append(kwargs["json"])
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {
            "status": "completed", "exit_code": 0, "stdout": "ok", "files": {"report.txt": base64.b64encode(b"result").decode()}})
    monkeypatch.setattr("httpx.post", post)
    monkeypatch.setattr(workspace_files, "upload_fileobj", lambda file, key, *_: stored.update({key: file.read()}))
    result = workspace_files.execute_tool("run_command", {"command": "python /skills/test/main.py"}, db,
        access, access.workspace_id, skill_packages={"test": {"main.py": b"print('ok')"}})
    assert result["exit_code"] == 0 and "files" not in result
    assert captured[0]["skill_files"] == {"test/main.py": base64.b64encode(b"print('ok')").decode()}
    assert list(stored.values()) == [b"result"]
    job = db.query(WorkspaceExecutionJob).one()
    assert job.changed_files == ["report.txt"]


def test_failed_output_save_rolls_back_metadata_and_allows_next_execution(accounts, monkeypatch):
    admin, db, _ = setup_site(accounts, monkeypatch)
    access = access_for(admin, db)
    monkeypatch.setattr(get_settings(), "workspace_executor_url", "http://runner.test")
    monkeypatch.setattr("httpx.post", lambda *args, **kwargs: SimpleNamespace(
        raise_for_status=lambda: None, json=lambda: {"status": "completed", "exit_code": 0,
            "files": {"report.txt": base64.b64encode(b"result").decode()}}))
    def unavailable(*args):
        raise HTTPException(503, "storage unavailable")
    monkeypatch.setattr(workspace_files, "upload_fileobj", unavailable)
    result = workspace_files.execute_tool("run_command", {"command": "echo result"}, db, access, access.workspace_id)
    assert result["status"] == "failed" and not result["generated_files"]
    assert db.query(WorkspaceFile).count() == db.query(WorkspaceFileVersion).count() == 0
    assert db.query(WorkspaceExecutionJob).one().status == "failed"
    monkeypatch.setattr(workspace_files, "upload_fileobj", lambda *args: None)
    result = workspace_files.execute_tool("run_command", {"command": "echo result"}, db, access, access.workspace_id)
    assert result["status"] == "completed" and len(result["generated_files"]) == 1
    row = db.query(WorkspaceFile).one()
    monkeypatch.setattr(workspace_files, "upload_fileobj", unavailable)
    with pytest.raises(HTTPException):
        workspace_files._save_bytes(access.workspace_id, access, db, row.path, b"changed", expected_version=1)
    db.commit()
    db.refresh(row)
    assert row.current_version == 1 and row.size_bytes == len(b"result")
    assert db.query(WorkspaceFileVersion).count() == 1


@pytest.mark.parametrize("path", ["../x", "/x", "a/../../x", "a\\x", "a:b", "a//x"])
def test_executor_rejects_unsafe_skill_paths(path):
    with pytest.raises(HTTPException):
        executor_service.input_archive({path: "eA=="}, skill=True)


def test_executor_mounts_packages_read_only_and_cleans_both_volumes(monkeypatch):
    calls = []
    def process(args, **kwargs):
        calls.append((args, kwargs))
        if "tar" in args and "-c" in args:
            data = io.BytesIO()
            with tarfile.open(fileobj=data, mode="w"):
                pass
            return subprocess.CompletedProcess(args, 0, data.getvalue(), b"")
        return subprocess.CompletedProcess(args, 0, "ok" if kwargs.get("text") else b"", "" if kwargs.get("text") else b"")
    monkeypatch.setattr(executor_service.subprocess, "run", process)
    result = executor_service.run(executor_service.RunRequest(workspace_id="test", command="python /skills/test/run.py",
        skill_files={"test/run.py": base64.b64encode(b"print('ok')").decode()}))
    assert result["exit_code"] == 0
    actual = next(args for args, _ in calls if "--name" in args)
    assert any("target=/skills,readonly=true" in part for part in actual)
    assert "--cap-drop=ALL" in actual and "--security-opt=no-new-privileges" in actual
    assert "--network=rag-workspace-runners" in actual
    assert len([args for args, _ in calls if args[1:3] == ["volume", "rm"]]) == 2


@pytest.mark.skipif(os.environ.get("RAG_TEST_REAL_EXECUTOR") != "1", reason="requires a local Docker runner image")
def test_real_container_runs_package_scripts_and_preserves_read_only_package():
    package = {
        "sample/assets/value.txt": b"REAL PACKAGE DATA",
        "sample/scripts/main.py": b"from pathlib import Path\np=Path(__file__).parents[1]/'assets/value.txt'\nPath('/workspace/python.txt').write_text(p.read_text())\n",
        "sample/scripts/main.cjs": b"const fs=require('fs');const path=require('path');fs.writeFileSync('/workspace/node.txt',fs.readFileSync(path.join(__dirname,'../assets/value.txt')));",
        "sample/scripts/main.sh": b"cat /skills/sample/assets/value.txt > /workspace/shell.txt\n",
    }
    encoded = {path: base64.b64encode(data).decode() for path, data in package.items()}
    result = executor_service.run(executor_service.RunRequest(workspace_id="test", skill_files=encoded,
        command="python /skills/sample/scripts/main.py && node /skills/sample/scripts/main.cjs && bash /skills/sample/scripts/main.sh"))
    assert result["exit_code"] == 0, result
    assert set(result["files"]) == {"python.txt", "node.txt", "shell.txt"}
    assert {base64.b64decode(value) for value in result["files"].values()} == {b"REAL PACKAGE DATA"}
    result = executor_service.run(executor_service.RunRequest(workspace_id="test", skill_files=encoded,
        command="echo changed > /skills/sample/assets/value.txt"))
    assert result["exit_code"] != 0 and not result["files"]


def test_network_setup_failure_never_starts_untrusted_command(monkeypatch):
    calls = []
    def process(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 1 if "--cap-add=NET_ADMIN" in args else 0, b"", b"")
    monkeypatch.setattr(executor_service.subprocess, "run", process)
    with pytest.raises(HTTPException) as error:
        executor_service.run(executor_service.RunRequest(workspace_id="test", command="echo untrusted"))
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
    result = executor_service.run(executor_service.RunRequest(workspace_id="test", command="python /skills/test/network.py",
        skill_files={"test/network.py": base64.b64encode(code.encode()).decode()}))
    assert result["exit_code"] == 0 and "PUBLIC_OK PRIVATE_BLOCKED" in result["stdout"], result
