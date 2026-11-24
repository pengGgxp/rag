import boto3
from tempfile import TemporaryFile
from fastapi import HTTPException

from app.infra.config import get_settings


def storage_backend():
    from app.infra.db import SessionLocal
    from app.infra.storage_backend import current_storage
    with SessionLocal() as db:
        return current_storage(db, require_active=False)


def client():
    settings = get_settings()
    return boto3.client(
        "s3",
        endpoint_url=settings.s3_endpoint,
        aws_access_key_id=settings.s3_access_key,
        aws_secret_access_key=settings.s3_secret_key,
        region_name=settings.s3_region,
        config=__import__("botocore").config.Config(s3={"addressing_style": "path"}),
    )


def upload_fileobj(fileobj, object_key: str, content_type: str) -> None:
    backend = storage_backend()
    if not backend:
        raise HTTPException(503, "文件存储尚未就绪，请联系站点管理员")
    backend.put(object_key, fileobj, content_type)


def download_fileobj(object_key: str):
    backend = storage_backend()
    if backend:
        return backend.download(object_key)
    settings = get_settings()
    output = TemporaryFile()
    client().download_fileobj(settings.s3_bucket, object_key, output)
    output.seek(0)
    return output


def download_range_fileobj(object_key: str, start: int, end: int):
    backend = storage_backend()
    if backend:
        return backend.download_range(object_key, start, end)
    settings = get_settings()
    return client().get_object(Bucket=settings.s3_bucket, Key=object_key,
        Range=f"bytes={start}-{end}")["Body"]


def head_object(object_key: str):
    backend = storage_backend()
    if backend:
        return backend.head(object_key)
    from botocore.exceptions import ClientError
    try:
        return client().head_object(Bucket=get_settings().s3_bucket, Key=object_key)
    except ClientError as exc:
        if str(exc.response.get("Error", {}).get("Code")) in {"404", "NoSuchKey", "NotFound"}:
            return None
        raise


def delete_object(object_key: str) -> None:
    backend = storage_backend()
    if backend:
        backend.delete(object_key)
        return
    settings = get_settings()
    try:
        client().delete_object(Bucket=settings.s3_bucket, Key=object_key)
    except Exception as exc:
        raise HTTPException(503, "对象存储删除失败，请稍后重试") from exc


def list_object_keys(prefix: str):
    backend = storage_backend()
    if backend:
        yield from (item["Key"] for item in backend.iter_objects(prefix))
        return
    for page in client().get_paginator("list_objects_v2").paginate(
        Bucket=get_settings().s3_bucket, Prefix=prefix
    ):
        yield from (item["Key"] for item in page.get("Contents", []))

