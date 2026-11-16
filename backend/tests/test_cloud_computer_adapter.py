import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import httpx
from e2b.sandbox_sync.filesystem.filesystem import FileStreamReader

from app.cloud_computer_adapter import AlibabaSandbox, DEPENDENCIES, LAUNCHER, PRIVATE_NETWORKS
from app.image_reading import ImageReadError


class FileDownload(httpx.SyncByteStream):
    def __init__(self, blocks, error=None):
        self.blocks = blocks
        self.error = error
        self.closed = False
        self.received = 0

    def __iter__(self):
        for block in self.blocks:
            self.received += 1
            yield block
        if self.error:
            raise self.error

    def close(self):
        self.closed = True


def streamed_file(blocks, error=None):
    download = FileDownload(blocks, error)
    response = httpx.Response(200, stream=download)
    reader = FileStreamReader(response)
    calls = []

    def read(path, **kwargs):
        calls.append((path, kwargs))
        if len(calls) > 1:
            return FileStreamReader(httpx.Response(200, stream=FileDownload(blocks, error)))
        return reader

    sandbox = SimpleNamespace(files=SimpleNamespace(read=read))
    return sandbox, download, response, calls


@pytest.mark.parametrize("blocks,limit,expected", [
    ([b"first", b"second", b"third"], 100, b"firstsecondthird"),
    ([], 10, b""),
    ([b"123", b"4567"], 7, b"1234567"),
    ([b"x" * (5 * 1024 * 1024)] * 2, 10 * 1024 * 1024, b"x" * (10 * 1024 * 1024)),
], ids=["multiple-blocks", "empty", "exact-limit", "exact-10mib"])
def test_read_file_consumes_real_sdk_stream_and_closes(blocks, limit, expected):
    sandbox, download, response, calls = streamed_file(blocks)
    provider = AlibabaSandbox.__new__(AlibabaSandbox)
    assert provider.read_file(sandbox, "/tmp/chart.png", max_bytes=limit) == expected
    assert len(calls) == 1 and calls[0][0] == "/tmp/chart.png"
    assert calls[0][1]["format"] == "stream" and calls[0][1]["user"] == "user"
    assert 0 < calls[0][1]["request_timeout"] <= 30
    assert 0 < calls[0][1]["stream_idle_timeout"] <= 30
    assert download.closed and response.is_closed


@pytest.mark.parametrize("blocks", [
    [b"123", b"45678", b"must not read"],
    [b"12345678", b"must not read"],
])
def test_read_file_stops_at_limit_and_closes_without_consuming_rest(blocks):
    sandbox, download, response, _ = streamed_file(blocks)
    provider = AlibabaSandbox.__new__(AlibabaSandbox)
    with pytest.raises(ValueError, match="大小限制"):
        provider.read_file(sandbox, "/tmp/chart.png", max_bytes=7)
    assert download.received == len(blocks) - 1
    assert download.closed and response.is_closed


@pytest.mark.parametrize("error", [httpx.ReadTimeout("private details"),
    httpx.ReadError("private details"), httpx.RemoteProtocolError("private details")])
def test_read_file_transfer_failure_is_safe_and_closes(error):
    sandbox, download, response, _ = streamed_file([b"partial"], error)
    provider = AlibabaSandbox.__new__(AlibabaSandbox)
    with pytest.raises(ImageReadError) as raised:
        provider.read_file(sandbox, "/tmp/chart.png", max_bytes=100)
    assert "private details" not in str(raised.value)
    assert download.closed and response.is_closed


def test_read_file_cancel_propagates_and_closes():
    from app.agent import AgentCancelled

    sandbox, download, response, _ = streamed_file([b"first", b"second"])
    provider = AlibabaSandbox.__new__(AlibabaSandbox)
    def check_active():
        if download.received:
            raise AgentCancelled()
    with pytest.raises(AgentCancelled):
        provider.read_file(sandbox, "/tmp/chart.png", max_bytes=100, check_active=check_active)
    assert download.received == 1 and download.closed and response.is_closed


@pytest.mark.parametrize("error", [PermissionError("denied"), AttributeError("bug")])
def test_read_file_does_not_swallow_permission_or_programming_errors(error):
    sandbox, download, response, _ = streamed_file([b"first"], error)
    provider = AlibabaSandbox.__new__(AlibabaSandbox)
    with pytest.raises(type(error)):
        provider.read_file(sandbox, "/tmp/chart.png", max_bytes=100)
    assert download.closed and response.is_closed


def test_read_file_handles_expected_sdk_failures_before_stream_opens():
    from e2b import AuthenticationException, FileNotFoundException, TimeoutException

    provider = AlibabaSandbox.__new__(AlibabaSandbox)
    for error in (FileNotFoundException("gone"), TimeoutException("timed out"),
            httpx.ConnectError("disconnected"), AuthenticationException("denied")):
        def read(*args, **kwargs):
            raise error
        sandbox = SimpleNamespace(files=SimpleNamespace(read=read))
        with pytest.raises(AuthenticationException if isinstance(error, AuthenticationException) else ImageReadError):
            provider.read_file(sandbox, "/tmp/chart.png", max_bytes=100)


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
    assert calls["create"]["allow_internet_access"] is True
    assert config["network"]["deny_out"] == PRIVATE_NETWORKS
    assert "allow_out" not in config["network"]
    assert "10.0.0.0/8" in config["network"]["deny_out"]
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
    provider.config = {"template": "fake", "max_seconds": 60, "blocked_hosts": ["blocked.example"],
        "oss_role_arn": "acs:ram::123:role/sandbox", "resource_mount": {
            "bucket": "test-bucket", "region": "cn-beijing",
            "prefix": "workspaces/workspace-a/resource-directory/", "mode": "shared", "file_tools": True}}
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
    assert calls["allow_internet_access"] is True
    assert calls["network"]["allow_out"] == ["test-bucket.oss-cn-beijing.aliyuncs.com"]
    assert calls["network"]["deny_out"] == PRIVATE_NETWORKS + ["blocked.example"]
    assert "10.0.0.0/8" in calls["network"]["deny_out"]
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
@pytest.mark.parametrize("size", [0, 50000, 524288, 1500000])
def test_launcher_bounds_stdout_and_stderr(tmp_path, size):
    launcher = tmp_path/"launcher.py"
    launcher.write_text(LAUNCHER)
    command = f"python3 -c \"import sys; sys.stdout.write('a'*{size}+'OUT-END'); sys.stderr.write('b'*{size}+'ERR-END')\""
    command += "\n" + "# long script line\n" * 1000
    (tmp_path/"input.json").write_text(json.dumps({"cwd": str(tmp_path), "command": command}))
    subprocess.run([sys.executable, str(launcher), str(tmp_path)], check=True, timeout=15)
    result = json.loads((tmp_path/"result.json").read_text())
    assert result["exit_code"] == 0
    for name, prefix, suffix in (("stdout", "a", "OUT-END"), ("stderr", "b", "ERR-END")):
        meta = result["output_capture"][name]
        assert meta["received_bytes"] == size + len(suffix)
        assert meta["retained_bytes"] == min(size + len(suffix), 512 * 1024)
        assert meta["omitted_bytes"] == max(0, size + len(suffix) - 512 * 1024)
        assert result[name].endswith(suffix)
        assert result[name].startswith(prefix if size else suffix)
        if meta["omitted_bytes"]:
            assert "discarded output cannot be recovered" in result[name]
            assert meta["head_bytes"] == meta["tail_bytes"] == 256 * 1024
        else:
            assert result[name] == prefix * size + suffix
    assert not (tmp_path/"stdout").exists()


def test_manifest_excludes_dependencies_and_retains_outputs(tmp_path):
    (tmp_path/"report.txt").write_text("synthetic")
    for directory in ("node_modules", ".venv", "__pycache__", ".cache", ".git"):
        path = tmp_path/directory
        path.mkdir()
        (path/"not-an-output.txt").write_text("dependency")
    from app.cloud_file_operation import perform
    assert [item["path"] for item in perform({"name": "list_files"}, tmp_path)["items"]] == ["report.txt"]
    assert not hasattr(AlibabaSandbox, "manifest")
