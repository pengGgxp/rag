"""Local provider/storage substitutions only; real app, broker and maintenance."""
import hashlib
import io
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import httpx

from app import cloud_computer as cloud, storage_backend
from app.main import app
from app.tasks import celery_app

ROOT = Path("/synthetic/objects")


class LocalStorage:
    bucket, region = "issue118-isolated", "cn-hangzhou"

    def path(self, key):
        target = (ROOT / key).resolve()
        assert target.is_relative_to(ROOT.resolve())
        return target

    def put(self, key, source, *_args):
        path = self.path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(source.read())

    def download(self, key):
        return self.path(key).open("rb")

    def download_range(self, key, start, end):
        with self.download(key) as handle:
            handle.seek(start)
            return io.BytesIO(handle.read(end - start + 1))

    def delete(self, key):
        self.path(key).unlink(missing_ok=True)

    def head(self, key):
        path = self.path(key)
        if not path.is_file():
            return None
        return {"Content-Length": path.stat().st_size,
                "ETag": hashlib.sha256(path.read_bytes()).hexdigest(),
                "Last-Modified": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)}

    def iter_objects(self, prefix=""):
        if not ROOT.exists():
            return
        for path in ROOT.rglob("*"):
            if path.is_file():
                key = path.relative_to(ROOT).as_posix()
                if key.startswith(prefix):
                    yield {"Key": key, "Size": path.stat().st_size, "ETag": self.head(key)["ETag"]}

    def incomplete_uploads(self):
        return []


class LocalProvider:
    def __init__(self, config):
        self.config = config

    def call(self, action, **arguments):
        response = httpx.post("http://mock:8900/provider/" + action,
            json={"config": self.config, **arguments}, timeout=35)
        response.raise_for_status()
        return response.json()

    def create(self, identifier):
        result = self.call("create", identifier=str(identifier))
        return result["id"], datetime.fromisoformat(result["started_at"])

    def find(self, identifier):
        return [(item["id"], datetime.fromisoformat(item["started_at"]))
                for item in self.call("find", identifier=str(identifier))]

    def connect(self, identifier):
        assert self.exists(identifier)
        return identifier

    def exists(self, identifier):
        return self.call("exists", identifier=identifier)

    def prepare(self, _sandbox):
        pass

    def kill(self, identifier):
        return self.call("kill", identifier=identifier)

    def start(self, sandbox, identifier, command):
        return self.call("start", sandbox=sandbox, identifier=str(identifier), command=command)

    def start_file(self, sandbox, identifier, operation):
        return self.call("file", sandbox=sandbox, identifier=str(identifier), operation=operation)

    def start_preview(self, sandbox, identifier, payload):
        return self.call("preview", sandbox=sandbox, identifier=str(identifier), payload=payload)

    def preview_target(self, sandbox, port, identifier):
        return f"synthetic-{sandbox}.preview.issue118.test", "synthetic-traffic-only"

    def preview_alive(self, sandbox, identifier):
        return self.call("preview_alive", sandbox=sandbox, identifier=str(identifier))

    def stop_preview(self, sandbox, identifier):
        self.call("cancel", sandbox=sandbox, identifier=str(identifier))

    def poll(self, sandbox, identifier):
        return self.call("poll", sandbox=sandbox, identifier=str(identifier))

    def file_result(self, sandbox, identifier):
        return self.call("file_result", sandbox=sandbox, identifier=str(identifier))

    def cancel(self, sandbox, identifier, pid):
        self.call("cancel", sandbox=sandbox, identifier=str(identifier))

    def read_file(self, sandbox, path, *, max_bytes, **_kwargs):
        relative = path.removeprefix("/home/user/workspace/")
        target = ROOT / self.config["resource_mount"]["prefix"] / relative
        data = target.read_bytes()
        assert len(data) <= max_bytes
        return data


store = LocalStorage()
storage_backend.active_settings = lambda _db: SimpleNamespace(
    provider="oss", bucket=store.bucket, region=store.region, active=True)
storage_backend.current_storage = lambda _db, **_kw: store
storage_backend.configured_backend = lambda _settings: store
cloud.adapter = lambda config, _key: LocalProvider(config)
