import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.cloud_computer_adapter import AlibabaSandbox, DEPENDENCIES, LAUNCHER, MANIFEST, PRIVATE_NETWORKS


def test_create_requires_network_isolation_and_connect_preserves_deadline():
    calls = {}
    started = datetime.now(timezone.utc)
    info = SimpleNamespace(started_at=started, end_at=started+timedelta(seconds=90))
    sandbox = SimpleNamespace(sandbox_id="synthetic", get_info=lambda: info)
    def create(**kwargs):
        calls["create"] = kwargs
        return sandbox
    def connect(identifier, **kwargs):
        calls["connect"] = kwargs
        return sandbox
    provider = AlibabaSandbox.__new__(AlibabaSandbox)
    provider.config = {"template": "fake", "max_seconds": 3600, "blocked_hosts": []}
    provider.options = {}
    provider.sdk = SimpleNamespace(create=create, get_info=lambda *_a, **_k: info, connect=connect)
    provider.create("synthetic-session")
    config = calls["create"]
    assert config["secure"] is True and config["timeout"] == 3600
    assert config["network"]["allow_public_traffic"] is False
    assert config["network"]["deny_out"] == PRIVATE_NETWORKS
    assert not {"zk.str1ct.top", "q.str1ct.top"}.intersection(config["network"]["deny_out"])
    provider.connect("synthetic")
    assert 80 <= calls["connect"]["timeout"] <= 85


def test_dependencies_use_runtime_tmp_volume_outside_outputs():
    calls = []
    sandbox = SimpleNamespace(
        commands=SimpleNamespace(run=lambda command, **kwargs: calls.append((command, kwargs)) or SimpleNamespace(pid=42)),
        files=SimpleNamespace(write=lambda *args, **kwargs: None))
    provider = AlibabaSandbox.__new__(AlibabaSandbox)
    provider.prepare(sandbox)
    assert DEPENDENCIES in calls[0][0]
    provider.start(sandbox, "test-command", "python3 -m pip install packaging")
    environment = calls[-1][1]["envs"]
    assert environment["PIP_TARGET"] == environment["PYTHONPATH"] == DEPENDENCIES
    assert DEPENDENCIES.startswith("/tmp/")


def test_mount_uses_workspace_directory_and_does_not_expose_storage_secrets():
    calls = {}
    provider = AlibabaSandbox.__new__(AlibabaSandbox)
    provider.config = {"template": "fake", "max_seconds": 60, "blocked_hosts": [],
        "oss_role_arn": "acs:ram::123:role/sandbox", "resource_mount": {
            "bucket": "test-bucket", "region": "cn-beijing",
            "prefix": "workspaces/workspace-a/resource-directory/", "mode": "shared"}}
    provider.options = {}
    def create(**kwargs):
        calls.update(kwargs)
        return SimpleNamespace(sandbox_id="fake", get_info=lambda: SimpleNamespace(started_at=None))
    provider.sdk = SimpleNamespace(create=create)
    provider.create("session-a")
    points = json.loads(calls["metadata"]["fc.sandbox.storage.oss"])["mountPoints"]
    assert points == [{"bucketName": "test-bucket", "mountDir": "/home/user/workspace",
        "bucketPath": "/workspaces/workspace-a/resource-directory",
        "endpoint": "https://oss-cn-beijing.aliyuncs.com", "readOnly": False}]
    assert calls["metadata"]["fc.sandbox.auth.role"] == "acs:ram::123:role/sandbox"
    assert calls["network"]["allow_out"] == ["test-bucket.oss-cn-beijing.aliyuncs.com", "8.8.8.8"]
    assert "access_key" not in json.dumps(calls)


def test_missing_launcher_result_does_not_wait_until_session_expiry():
    provider = AlibabaSandbox.__new__(AlibabaSandbox)
    sandbox = SimpleNamespace(
        files=SimpleNamespace(exists=lambda path, **kwargs: path.endswith("launcher-pid"),
            read=lambda *args, **kwargs: "42"),
        commands=SimpleNamespace(list=lambda: []))
    with pytest.raises(RuntimeError, match="without a durable result"):
        provider.poll(sandbox, "synthetic-command")


@pytest.mark.skipif(sys.platform == "win32", reason="launcher requires the Linux bash template")
def test_launcher_bounds_stdout_and_stderr(tmp_path):
    launcher = tmp_path/"launcher.py"
    launcher.write_text(LAUNCHER)
    (tmp_path/"input.json").write_text(json.dumps({"cwd": str(tmp_path), "command": "python3 -c \"import sys; sys.stdout.write('a'*50000); sys.stderr.write('b'*50000)\""}))
    subprocess.run([sys.executable, str(launcher), str(tmp_path)], check=True, timeout=15)
    result = json.loads((tmp_path/"result.json").read_text())
    assert result == {"exit_code": 0, "stdout": "a"*12000, "stderr": "b"*12000}
    assert not (tmp_path/"stdout").exists()


def test_manifest_excludes_dependencies_and_retains_outputs(tmp_path):
    (tmp_path/"report.txt").write_text("synthetic")
    for directory in ("node_modules", ".venv", "__pycache__", ".cache", ".git"):
        path = tmp_path/directory
        path.mkdir()
        (path/"not-an-output.txt").write_text("dependency")
    script = MANIFEST.replace('Path("/home/user/workspace")', f"Path({str(tmp_path)!r})")
    result = subprocess.run([sys.executable, "-c", script], check=True, timeout=10, capture_output=True, text=True)
    assert list(json.loads(result.stdout)) == ["report.txt"]
