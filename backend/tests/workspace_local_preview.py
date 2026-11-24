"""Explicit local-only storage injection for Issue #100 browser acceptance.

Run only inside the disposable Docker project. There is no production fallback.
"""
import io
import os
import hashlib
from pathlib import Path
from contextlib import asynccontextmanager
from tempfile import TemporaryFile

import boto3
from botocore.exceptions import ClientError
from fastapi import HTTPException


class LocalS3:
    conditional_create = True
    region = "us-east-1"

    def __init__(self):
        from app.infra.config import get_settings
        config = get_settings()
        if os.environ.get("ISSUE100_LOCAL_PREVIEW") != "1" or config.s3_endpoint != "http://minio:9000":
            raise RuntimeError("refusing non-local preview storage")
        self.bucket = "issue100-workspace-acceptance"
        self.client = boto3.client("s3", endpoint_url=config.s3_endpoint, region_name=self.region,
            aws_access_key_id=config.s3_access_key, aws_secret_access_key=config.s3_secret_key)
        try:
            self.client.create_bucket(Bucket=self.bucket)
        except ClientError as error:
            if error.response["Error"]["Code"] not in ("BucketAlreadyOwnedByYou", "BucketAlreadyExists"):
                raise

    def head(self, key):
        try:
            data = self.client.head_object(Bucket=self.bucket, Key=key)
            return {"ETag": data["ETag"], "Content-Length": data["ContentLength"], "Content-Type": data.get("ContentType"),
                "Last-Modified": str(data.get("LastModified", ""))}
        except ClientError as error:
            if error.response["Error"]["Code"] in ("404", "NoSuchKey"):
                return None
            raise

    def put(self, key, source, content_type="application/octet-stream", *, create=False):
        try:
            return self.client.put_object(Bucket=self.bucket, Key=key, Body=source.read(), ContentType=content_type,
                **({"IfNoneMatch": "*"} if create else {}))
        except ClientError as error:
            if error.response["ResponseMetadata"]["HTTPStatusCode"] in (409, 412):
                raise HTTPException(409, "文件已存在，请刷新后重试") from None
            raise

    def download(self, key):
        return self.download_range(key, None, None)

    def download_range(self, key, start, end):
        body = self.client.get_object(Bucket=self.bucket, Key=key,
            **({"Range": f"bytes={start}-{end}"} if start is not None else {}))["Body"]
        target = TemporaryFile()
        try:
            for block in iter(lambda: body.read(1024 * 1024), b""):
                target.write(block)
            target.seek(0)
            return target
        finally:
            body.close()

    def copy(self, source, target, *, etag=None, create=False):
        # Local S3 adapter intentionally uses choice-A source/target checks.
        # It does not claim to verify the production OSS implementation.
        metadata = self.head(source)
        if not metadata or etag and metadata["ETag"] != etag:
            raise HTTPException(409, "来源已变化，请刷新")
        with self.download(source) as body:
            return self.put(target, body, metadata.get("Content-Type") or "application/octet-stream", create=create)

    def delete(self, key):
        self.client.delete_object(Bucket=self.bucket, Key=key)

    def iter_objects(self, prefix=None):
        for page in self.client.get_paginator("list_objects_v2").paginate(Bucket=self.bucket, Prefix=prefix or ""):
            for item in page.get("Contents", []):
                yield {"Key": item["Key"], "Size": item["Size"], "ETag": item["ETag"]}


class LocalFiles:
    """Explicit disposable local object adapter, shared only by the test containers."""
    conditional_create = True
    def __init__(self):
        if os.environ.get("ISSUE100_LOCAL_PREVIEW") != "1":
            raise RuntimeError("local preview must be explicitly enabled")
        self.root = Path("/test-objects")
        self.root.mkdir(parents=True, exist_ok=True)
    def path(self, key):
        path = (self.root / key).resolve()
        if not path.is_relative_to(self.root.resolve()):
            raise RuntimeError("invalid local object key")
        return path
    def head(self, key):
        path = self.path(key)
        if not path.is_file() or path.is_symlink():
            return None
        return {"ETag": hashlib.sha256(path.read_bytes()).hexdigest(), "Content-Length": path.stat().st_size}
    def put(self, key, source, content_type="application/octet-stream", *, create=False):
        path = self.path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with path.open("xb" if create else "wb") as output:
                output.write(source.read())
        except FileExistsError:
            raise HTTPException(409, "文件已存在，请刷新后重试") from None
    def download(self, key):
        return self.path(key).open("rb")
    def download_range(self, key, start, end):
        with self.download(key) as body:
            if start is None:
                return io.BytesIO(body.read())
            body.seek(start)
            return io.BytesIO(body.read(end - start + 1))
    def copy(self, source, target, *, etag=None, create=False):
        metadata = self.head(source)
        if not metadata or etag and metadata["ETag"] != etag:
            raise HTTPException(409, "来源已变化，请刷新")
        with self.download(source) as body:
            self.put(target, body, create=create)
    def delete(self, key):
        self.path(key).unlink(missing_ok=True)
    def iter_objects(self, prefix=None):
        for path in sorted(self.root.rglob("*")):
            if path.is_file() and not path.is_symlink():
                key = path.relative_to(self.root).as_posix()
                if key.startswith(prefix or ""):
                    data = self.head(key)
                    yield {"Key": key, "ETag": data["ETag"], "Size": data["Content-Length"]}


def build_app():
    from sqlalchemy.engine import make_url
    from app.infra.config import get_settings
    database = make_url(get_settings().database_url)
    if (os.environ.get("ISSUE100_LOCAL_PREVIEW") != "1" or database.host != "db"
            or not (database.database or "").startswith("rag_auth_test_issue100_browser_")):
        raise RuntimeError("refusing non-disposable preview database")
    from app import main
    from app.files import shared as shared_resources, downloads as storage_downloads, cache as resource_cache
    from app.infra import storage_backend, storage
    from app.skills import discovery as skill_discovery
    from app.cloud_computer import runtime as cloud_computer
    local = LocalFiles() if os.environ.get("ISSUE100_LOCAL_STORE") == "filesystem" else LocalS3()
    shared_resources.backend = lambda db: local
    storage_backend.current_storage = lambda db: local
    storage.download_fileobj = local.download
    storage.head_object = local.head
    storage_downloads.download_range_fileobj = local.download_range
    resource_cache.download_range_fileobj = local.download_range
    def never_paid(*args, **kwargs):
        raise AssertionError("paid execution forbidden in local acceptance")
    cloud_computer.submit = never_paid
    cloud_computer.execute = never_paid
    @asynccontextmanager
    async def lifespan(_):
        yield
    main.app.router.lifespan_context = lifespan
    return main.app


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(build_app(), host="0.0.0.0", port=8000)
