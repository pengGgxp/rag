"""Selected Skill packages reach a read-only runner mount, never shared output storage."""
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
from app.models import (AgentSkill, AgentSkillFile, ChatMessage, Conversation, Membership, SiteSkill,
    WorkspaceExecutionJob, WorkspaceFile, WorkspaceFileVersion)
from test_extensions import access_for
from test_personal_extensions import skill
from test_shared_providers import setup_site


def enable_cloud(db, access, monkeypatch):
    from test_cloud_computer import enable_execution
    return enable_execution(db, access, monkeypatch)


def scripted(client, **changes):
    return skill(client, limitations_acknowledged=True, files=[
        {"path": "scripts/task.py", "content": base64.b64encode(b"print('PINNED SCRIPT')").decode()},
        {"path": "assets/value.bin", "content": base64.b64encode(b"\xff\x00").decode()},
    ], **changes)


def test_runtime_passes_available_pinned_packages_without_selection_or_loading(accounts, monkeypatch):
    admin, db, _ = setup_site(accounts, monkeypatch)
    enable_cloud(db, access_for(admin, db), monkeypatch)
    monkeypatch.setattr(get_settings(), "workspace_executor_url", "http://runner.test")
    first, second = scripted(admin), scripted(admin, name="Second")
    scripted(admin, name="Disabled", enabled=False)
    (_, member, _), _ = accounts
    from test_auth import register
    register(member, "other-script-owner@example.com")
    foreign = scripted(member, name="Other owner")
    # Even within the same workspace, another member's private package stays private.
    db.get(AgentSkill, UUID(foreign["id"])).workspace_id = access_for(admin, db).workspace_id
    db.commit()
    runtime = ExtensionRuntime(lambda: nullcontext(db), access_for(admin, db))
    assert "run_command" in {item["function"]["name"] for item in runtime.tools}
    captured = []
    monkeypatch.setattr(extensions, "execute_tool", lambda *args, **kwargs: captured.append(kwargs) or {"exit_code": 0})
    assert runtime.execute("run_command", {"command": "echo ready"})["exit_code"] == 0
    assert captured.pop()["skill_packages"] == {}
    assert runtime.execute("run_command", {"command": f"python /skills/{first['id']}/scripts/task.py"})["exit_code"] == 0
    assert set(captured.pop()["skill_packages"]) == {first["id"]}
    loaded = runtime.execute("load_skill", {"skill_id": first["id"]})
    assert loaded["execute_scripts"] and loaded["execution"]["directory"] == f"/skills/{first['id']}"
    assert loaded["execution"]["read_only"] and loaded["execution"]["scripts"] == ["scripts/task.py"]
    row = db.get(AgentSkill, UUID(first["id"]))
    row.files[0].content = b"CHANGED AFTER REQUEST"
    row.version += 1
    db.commit()
    assert runtime.execute("run_command", {"command": "python script.py", "skill_ids": [first["id"]]})["exit_code"] == 0
    package = captured[0]["skill_packages"]
    assert set(package) == {first["id"]} and package[first["id"]]["scripts/task.py"] == b"print('PINNED SCRIPT')"
    assert package[first["id"]]["assets/value.bin"] == b"\xff\x00"
    assert runtime.execute("run_command", {"command": "x", "skill_files": {}}).get("error")
    row.enabled = False
    db.commit()
    assert runtime.execute("run_command", {"command": "echo unrelated task"})["exit_code"] == 0
    assert captured[-1]["skill_packages"] == {}
    assert len(captured) == 2


def test_missing_executor_does_not_advertise_execution(accounts, monkeypatch):
    admin, db, _ = setup_site(accounts, monkeypatch)
    monkeypatch.setattr(get_settings(), "workspace_executor_url", "")
    row = scripted(admin)
    runtime = ExtensionRuntime(lambda: nullcontext(db), access_for(admin, db), [row["id"]])
    assert "run_command" not in {item["function"]["name"] for item in runtime.tools}
    assert not runtime.execute("load_skill", {"skill_id": row["id"]})["execute_scripts"]
    assert runtime.execute("run_command", {"command": "echo no"}).get("error")


def test_output_tools_require_cloud_and_recheck_role(accounts, monkeypatch):
    admin, db, _ = setup_site(accounts, monkeypatch)
    monkeypatch.setattr(get_settings(), "workspace_executor_url", "")
    stored = {}
    monkeypatch.setattr(workspace_files, "upload_fileobj", lambda stream, key, *_: stored.update({key: stream.read()}))
    monkeypatch.setattr(workspace_files, "download_fileobj", lambda key: io.BytesIO(stored[key]))
    monkeypatch.setattr("app.resource_cache.download_range_fileobj", lambda key, start, end: io.BytesIO(stored[key][start:end+1]))
    access = access_for(admin, db)
    runtime = ExtensionRuntime(lambda: nullcontext(db), access, [])
    assert "write_file" not in {item["function"]["name"] for item in runtime.tools}
    assert runtime.execute("list_files", {})["error_code"] == "file_tool_unavailable"
    assert not stored
    assert runtime.execute("run_command", {"command": "echo no"})["error_code"] == "file_tool_unavailable"
    enable_cloud(db, access, monkeypatch)
    runtime = ExtensionRuntime(lambda: nullcontext(db), access, [])
    assert "write_file" in {item["function"]["name"] for item in runtime.tools}
    member = db.get(Membership, (access.workspace_id, access.user_id))
    member.role = "viewer"
    db.commit()
    with pytest.raises(HTTPException) as error:
        runtime.execute("write_file", {"path": "second.txt", "content": "forbidden"})
    assert error.value.status_code == 403 and not stored


def test_output_before_skill_load_and_safe_failure_recovery(accounts, monkeypatch):
    admin, db, _ = setup_site(accounts, monkeypatch)
    enable_cloud(db, access_for(admin, db), monkeypatch)
    monkeypatch.setattr(get_settings(), "workspace_executor_url", "http://runner.test")
    selected = scripted(admin)
    runtime = ExtensionRuntime(lambda: nullcontext(db), access_for(admin, db), [selected["id"]])
    captured = []
    with monkeypatch.context() as patch:
        patch.setattr(extensions, "execute_tool", lambda *args, **kwargs: captured.append(kwargs) or {"exit_code": 0})
        assert runtime.execute("run_command", {"command": "echo ready", "skill_ids": [selected["id"]]})["exit_code"] == 0
        assert set(captured[0]["skill_packages"]) == {selected["id"]}
    def unavailable(*args, **kwargs):
        raise HTTPException(503, "云电脑尚未开放，请联系管理员")
    monkeypatch.setattr("app.cloud_computer.execute", unavailable)
    result = runtime.execute("write_file", {"path": "report.txt", "content": "hello"})
    assert result["error_code"] == "cloud_computer_unavailable"
    assert db.query(WorkspaceFile).count() == 0
    assert runtime.execute("write_file", {"path": "report.txt", "content": "changed", "expected_version": 9})["error_code"] == "file_arguments"
    assert runtime.execute("write_file", {"path": "../forbidden", "content": "bad"})["error_code"] == "file_arguments"
    assert runtime.execute("write_file", {"path": "report.txt", "content": "bad", "expected_version": 0})["error_code"] == "file_arguments"


def test_command_without_personal_skill_and_role_revocation(accounts, monkeypatch):
    from test_extensions import create
    admin, db, _ = setup_site(accounts, monkeypatch)
    monkeypatch.setattr(get_settings(), "workspace_executor_url", "http://runner.test")
    access = access_for(admin, db)
    enable_cloud(db, access, monkeypatch)
    captured = []
    def runner(*args, **kwargs):
        captured.append(kwargs)
        return {"exit_code": 0}
    monkeypatch.setattr(extensions, "execute_tool", runner)
    for platform in (False, True):
        if platform:
            method = create(admin)
        runtime = ExtensionRuntime(lambda: nullcontext(db), access)
        if platform:
            assert "instructions" in runtime.execute("load_skill", {"skill_id": method["id"]})
        assert runtime.execute("run_command", {"command": "echo ready"})["exit_code"] == 0
        assert captured[-1]["skill_packages"] == {}
    member = db.get(Membership, (access.workspace_id, access.user_id))
    member.role = "viewer"
    db.commit()
    with pytest.raises(HTTPException) as error:
        runtime.execute("run_command", {"command": "echo forbidden"})
    assert error.value.status_code == 403 and len(captured) == 2
    viewer = ExtensionRuntime(lambda: nullcontext(db), access)
    assert "run_command" not in {item["function"]["name"] for item in viewer.tools}


def test_platform_package_mounts_by_skill_id_and_command_path_with_pinned_bytes(accounts, monkeypatch):
    from test_extensions import create
    admin, db, _ = setup_site(accounts, monkeypatch)
    access = access_for(admin, db)
    fake, _ = enable_cloud(db, access, monkeypatch)
    skill = create(admin, files=[
        {"path": "scripts/task.py", "content": base64.b64encode(b"print('PLATFORM')").decode()},
        {"path": "assets/value.bin", "content": base64.b64encode(b"\xff\x00").decode()},
    ])
    conversation = Conversation(workspace_id=access.workspace_id, owner_id=access.user_id)
    db.add(conversation); db.flush()
    message = ChatMessage(conversation_id=conversation.id, sequence=1, role="assistant", content="")
    db.add(message); db.commit()
    runtime = ExtensionRuntime(lambda: nullcontext(db), access, message_id=message.id, request_id=uuid4())
    loaded = runtime.execute("load_skill", {"skill_id": skill["id"]})
    assert loaded["execute_scripts"] and loaded["resources"] == [{"path": "scripts/task.py"}]
    assert loaded["execution"]["directory"] == f"/skills/{skill['id']}"
    assert loaded["execution"]["scripts"] == ["scripts/task.py"] and loaded["execution"]["read_only"]
    written = []
    monkeypatch.setattr(fake, "write_skill", lambda sandbox, path, data: written.append((path, data)))
    result = runtime.execute("run_command", {"command": "echo ready", "skill_ids": [skill["id"]]})
    assert result["exit_code"] == 0
    assert fake.skill_clears == 1
    assert dict(written) == {f"{skill['id']}/SKILL.md": skill["markdown"].encode(),
        f"{skill['id']}/assets/value.bin": b"\xff\x00", f"{skill['id']}/scripts/task.py": b"print('PLATFORM')"}
    written.clear()
    assert runtime.execute("run_command", {"command": f"python /skills/{skill['id']}/scripts/task.py"})["exit_code"] == 0
    assert fake.skill_clears == 2
    assert {path for path, _ in written} == {f"{skill['id']}/SKILL.md", f"{skill['id']}/assets/value.bin",
        f"{skill['id']}/scripts/task.py"}
    row = db.get(SiteSkill, UUID(skill["id"]))
    row.files[0].content, row.version = b"CHANGED", 2
    db.commit()
    written.clear()
    runtime.request_id = uuid4()
    runtime.execute("run_command", {"command": f"python /skills/{skill['id']}/scripts/task.py changed"})
    assert (f"{skill['id']}/scripts/task.py", b"print('PLATFORM')") in written
    row.enabled = False
    db.commit()
    assert runtime.execute("run_command", {"command": f"python /skills/{skill['id']}/scripts/task.py"})["error_code"] == "skill_missing"


def test_platform_and_personal_uuid_collision_is_rejected(accounts, monkeypatch):
    from test_extensions import create
    from test_personal_extensions import skill
    admin, db, _ = setup_site(accounts, monkeypatch)
    platform = create(admin)
    personal = skill(admin, limitations_acknowledged=True)
    private = db.get(AgentSkill, UUID(personal["id"]))
    private.id = UUID(platform["id"])
    db.commit()
    with pytest.raises(HTTPException) as error:
        ExtensionRuntime(lambda: nullcontext(db), access_for(admin, db))
    assert error.value.status_code == 409 and "标识冲突" in str(error.value.detail)


@pytest.mark.parametrize("path", ["not-a-skill", str(uuid4()), "scripts"])
def test_unknown_command_skill_path_is_rejected_before_execution(accounts, monkeypatch, path):
    admin, db, _ = setup_site(accounts, monkeypatch)
    enable_cloud(db, access_for(admin, db), monkeypatch)
    runtime = ExtensionRuntime(lambda: nullcontext(db), access_for(admin, db))
    monkeypatch.setattr(extensions, "execute_tool", lambda *args, **kwargs: pytest.fail("command submitted"))
    result = runtime.execute("run_command", {"command": f"python /skills/{path}/task.py"})
    assert result["error_code"] == "skill_missing"


def test_command_skill_limit_includes_ids_and_paths(accounts, monkeypatch):
    admin, db, _ = setup_site(accounts, monkeypatch)
    enable_cloud(db, access_for(admin, db), monkeypatch)
    skills = [scripted(admin, name=f"Skill {index}") for index in range(20)]
    selected = [item["id"] for item in skills]
    selected.append(scripted(admin, name="Path only")["id"])
    runtime = ExtensionRuntime(lambda: nullcontext(db), access_for(admin, db))
    monkeypatch.setattr(extensions, "execute_tool", lambda *args, **kwargs: pytest.fail("command submitted"))
    result = runtime.execute("run_command", {"command": f"python /skills/{selected[-1]}/scripts/task.py",
        "skill_ids": selected[:-1]})
    assert result["error_code"] == "skill_limit"


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


def test_run_command_forwards_packages_and_saves_only_outputs(accounts, monkeypatch):
    admin, db, _ = setup_site(accounts, monkeypatch)
    access = access_for(admin, db)
    fake, execute = enable_cloud(db, access, monkeypatch)
    captured, stored = [], {}
    fake.outputs = {"report.txt": b"result"}
    monkeypatch.setattr(fake, "write_skill", lambda sandbox, path, data: captured.append((path, data)))
    monkeypatch.setattr(workspace_files, "upload_fileobj", lambda file, key, *_: stored.update({key: file.read()}))
    result = execute({"command": "python /skills/test/main.py"}, {"test": {"main.py": b"print('ok')"}})
    assert result["exit_code"] == 0 and "files" not in result
    assert captured == [("test/main.py", b"print('ok')")]
    assert not stored
    assert admin.get("/resources").json()["items"][0]["path"] == "report.txt"
    from app.models import CloudComputerCommand
    assert not db.query(CloudComputerCommand).one().result.get("generated_files")


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
    result = executor_service.run(executor_service.RunRequest(workspace_id="test", command="true"))
    assert base64.b64decode(result["files"]["large.bin"]) == payload
    assert base64.b64decode(result["files"][".result.txt"]) == b"hidden"


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
