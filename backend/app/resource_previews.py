"""Rebuildable previews. Object bytes never become resources or billable internal reads."""
import hashlib
import io
import json
import logging
import os
import subprocess
import sys
from datetime import timedelta
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from .auth import Access, aware, get_access, now
from .db import SessionLocal, get_db
from .models import ResourcePreview, WorkspaceFile
from .shared_resources import object_key
from .storage_backend import current_storage
from .workspace_files import owned

router = APIRouter(prefix="/resources", tags=["resource-previews"])
RENDERER = "preview-1"
PREFIX = "preview-cache/"
MAX_WORD = 50 * 1024 * 1024
MAX_SOURCE = 200 * 1024 * 1024
logger = logging.getLogger(__name__)


def file_kind(row):
    suffix = PurePosixPath(row.path).suffix.lower()
    if row.content_type in {"image/png", "image/jpeg", "image/webp", "image/gif"}:
        return "image"
    if suffix == ".pdf":
        return "pdf"
    if suffix in {".doc", ".docx"}:
        return "word"
    return "text" if row.content_type.startswith("text/") or suffix in {".md", ".json", ".svg"} else "unsupported"


def fingerprint(row, metadata):
    value = [str(row.id), metadata.get("ETag"), metadata.get("Last-Modified"),
        metadata.get("Content-Length", metadata.get("ContentLength", row.size_bytes)), RENDERER]
    if not value[1] and not value[2]:
        value.append(row.updated_at.isoformat())
    return hashlib.sha256(json.dumps(value, default=str).encode()).hexdigest()


def current(db, access, identifier):
    row = owned(db, access, access.workspace_id, identifier)
    storage = current_storage(db)
    metadata = storage.head(object_key(row.workspace_id, row.path))
    if metadata is None:
        raise HTTPException(404, "文件已删除或不可用，请刷新资源库")
    revision = fingerprint(row, metadata)
    row.size_bytes = int(metadata.get("Content-Length", metadata.get("ContentLength", row.size_bytes)))
    if metadata.get("ETag") and row.live_etag != metadata["ETag"]:
        row.live_etag, row.updated_at = metadata["ETag"], now()
    db.commit()
    return row, storage, revision


def describe(row):
    return {"id": str(row.id), "kind": row.kind, "status": row.status, "error": row.error,
        "width": row.width, "height": row.height, "size_bytes": row.size_bytes,
        "url": f"/resources/{row.resource_id}/preview/{row.id}/content" if row.status == "ready" else None}


@router.get("/{identifier}/preview")
def detail(identifier: UUID, db=Depends(get_db), access: Access=Depends(get_access)):
    row, storage, revision = current(db, access, identifier)
    previews = list(db.scalars(select(ResourcePreview).where(ResourcePreview.resource_id == row.id,
        ResourcePreview.revision == revision)))
    for item in previews:
        item.accessed_at = now()
        if item.status == "ready" and not storage.head(item.object_key):
            item.status, item.object_key, item.size_bytes = "expired", None, 0
    db.commit()
    return {"revision": revision, "kind": file_kind(row), "size_bytes": row.size_bytes,
        "previews": [describe(item) for item in previews],
        "original_url": f"/resources/{row.id}/preview/original?revision={revision}"}


class Prepare(BaseModel):
    kind: str = "thumbnail"
    retry: bool = False


def dispatch(identifier):
    from .tasks import render_resource_preview
    try:
        render_resource_preview.apply_async(args=[str(identifier)], queue="previews")
    except Exception:
        # Queued intent is durable; maintenance retries delivery.
        pass


@router.post("/{identifier}/preview", status_code=202)
def prepare(identifier: UUID, payload: Prepare, db=Depends(get_db), access: Access=Depends(get_access)):
    row, _, revision = current(db, access, identifier)
    kind = file_kind(row)
    if payload.kind not in {"thumbnail", "document"} or kind not in {"image", "pdf", "word"}:
        raise HTTPException(415, "该文件暂不支持内容预览，请下载查看")
    if payload.kind == "document" and kind != "word":
        raise HTTPException(422, "该文件无需转换")
    # Word is converted only after the user opens it, never by the thumbnail grid.
    if kind == "word" and payload.kind == "thumbnail":
        existing = db.scalar(select(ResourcePreview).where(ResourcePreview.resource_id == row.id,
            ResourcePreview.revision == revision, ResourcePreview.kind == "thumbnail"))
        return describe(existing) if existing else {"status": "not_prepared", "kind": "thumbnail"}
    if kind == "word" and row.size_bytes > MAX_WORD:
        raise HTTPException(413, "Word 预览支持 50 MiB 以内的文件，请下载查看")
    item = db.scalar(select(ResourcePreview).where(ResourcePreview.resource_id == row.id,
        ResourcePreview.revision == revision, ResourcePreview.kind == payload.kind).with_for_update())
    if item is None:
        item = ResourcePreview(workspace_id=row.workspace_id, resource_id=row.id, revision=revision,
            kind=payload.kind, status="queued")
        db.add(item)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            item = db.scalar(select(ResourcePreview).where(ResourcePreview.resource_id == row.id,
                ResourcePreview.revision == revision, ResourcePreview.kind == payload.kind))
    elif payload.retry and item.status in {"failed", "expired"}:
        item.status, item.error = "queued", None
        db.commit()
    if item.status == "queued":
        dispatch(item.id)
    return describe(item)


@router.api_route("/{identifier}/preview/original", methods=["GET", "HEAD"])
def original(identifier: UUID, revision: str, request: Request, db=Depends(get_db), access: Access=Depends(get_access)):
    row, _, actual = current(db, access, identifier)
    if actual != revision:
        raise HTTPException(409, "文件已更新，请重新打开预览")
    if file_kind(row) not in {"image", "pdf", "text"}:
        raise HTTPException(415, "该格式需要准备预览")
    from .storage_downloads import download_response
    content_type = "text/plain" if file_kind(row) == "text" else row.content_type
    return download_response(db, access, request, object_key=object_key(row.workspace_id, row.path),
        size=row.size_bytes, filename=PurePosixPath(row.path).name, content_type=content_type,
        resource=f"user_download:{row.id}", inline=True)


@router.api_route("/{identifier}/preview/{preview_id}/content", methods=["GET", "HEAD"])
def content(identifier: UUID, preview_id: UUID, request: Request, db=Depends(get_db), access: Access=Depends(get_access)):
    row, storage, revision = current(db, access, identifier)
    item = db.get(ResourcePreview, preview_id)
    if not item or item.resource_id != row.id or item.workspace_id != access.workspace_id:
        raise HTTPException(404, "预览不存在")
    if item.revision != revision:
        raise HTTPException(409, "文件已更新，请重新打开预览")
    if item.status != "ready":
        raise HTTPException(410, "预览缓存已过期，请重新打开")
    if not storage.head(item.object_key):
        item.status, item.object_key, item.size_bytes = "expired", None, 0
        db.commit()
        raise HTTPException(410, "预览缓存已过期，请重新打开")
    item.accessed_at = now(); db.commit()
    from .storage_downloads import download_response
    return download_response(db, access, request, object_key=item.object_key, size=item.size_bytes,
        filename="preview.pdf" if item.kind == "document" else "thumbnail.webp",
        content_type="application/pdf" if item.kind == "document" else "image/webp",
        resource=f"user_download:preview:{row.id}", inline=True)


def generate(source, folder, kind, file_type):
    """Run codecs and Office without platform credentials, network or write access to app files."""
    import shutil
    script = Path(__file__).with_name("preview_renderer.py")
    renderer = folder / "renderer.py"
    shutil.copyfile(script, renderer)
    args = [sys.executable, str(renderer), str(source), str(folder), kind, file_type]
    options = {"env": {"PATH": os.environ.get("PATH", ""), "HOME": str(folder), "TMPDIR": str(folder),
        "LANG": "C.UTF-8", "SAL_USE_VCLPLUGIN": "svp", "OSL_SOCKET_PATH": str(folder)}, "cwd": folder}
    if sys.platform == "linux":
        os.chmod(folder, 0o777); os.chmod(source, 0o644)
        if os.getuid() == 0:
            options.update(user=65534, group=65534, extra_groups=[])
    elif file_type == "word":
        raise RuntimeError("Word 预览需要 Linux 预览 Worker，请下载查看")
    process = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        start_new_session=sys.platform == "linux", **options)
    try:
        _, diagnostics = process.communicate(timeout=125)
    except subprocess.TimeoutExpired:
        if sys.platform == "linux":
            import signal
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
        process.communicate()
        raise RuntimeError("预览准备超时，请下载查看") from None
    if process.returncode:
        raise RuntimeError("文件无法预览，可能已加密、损坏或格式不受支持，请下载查看")
    output = folder / ("document.pdf" if kind == "document" else "thumbnail.webp")
    if not output.exists() or output.stat().st_size > 200 * 1024 * 1024:
        raise RuntimeError("预览内容超出处理限制，请下载查看")
    thumb = folder / "thumbnail.webp"
    from PIL import Image
    with Image.open(thumb) as image:
        dimensions = image.size
    return output, dimensions, thumb


def run(identifier, factory=SessionLocal):
    with factory() as db:
        item = db.scalar(select(ResourcePreview).where(ResourcePreview.id == UUID(str(identifier))).with_for_update())
        if not item or item.status != "queued":
            return
        item.status, item.updated_at = "running", now()
        db.commit()
        key = None
        try:
            storage = current_storage(db)
            row = db.get(WorkspaceFile, item.resource_id, populate_existing=True)
            if not row or row.deleted_at:
                raise RuntimeError("资源已删除")
            source_key = object_key(row.workspace_id, row.path)
            metadata = storage.head(source_key)
            if not metadata or fingerprint(row, metadata) != item.revision:
                raise RuntimeError("文件已更新，请重新打开预览")
            if row.size_bytes > MAX_SOURCE or (file_kind(row) == "word" and row.size_bytes > MAX_WORD):
                raise RuntimeError("文件超出预览大小限制，请下载查看")
            with TemporaryDirectory(prefix="rag-preview-") as temporary:
                folder = Path(temporary)
                source = folder / ("input" + PurePosixPath(row.path).suffix.lower())
                stream = storage.download(source_key)
                try:
                    with source.open("wb") as target:
                        limit = MAX_WORD if file_kind(row) == "word" else MAX_SOURCE
                        received = 0
                        while block := stream.read(min(1024 * 1024, limit-received+1)):
                            received += len(block)
                            if received > limit:
                                raise RuntimeError("文件超出预览大小限制，请下载查看")
                            target.write(block)
                finally:
                    stream.close()
                if source.stat().st_size > 200 * 1024 * 1024 or (file_kind(row) == "word" and source.stat().st_size > MAX_WORD):
                    raise RuntimeError("文件超出预览大小限制，请下载查看")
                output, dimensions, thumb = generate(source, folder, item.kind, file_kind(row))
                db.refresh(row)
                after = storage.head(source_key)
                if row.deleted_at or not after or fingerprint(row, after) != item.revision:
                    raise RuntimeError("文件已更新或删除，请重新打开预览")
                key = f"{PREFIX}{item.workspace_id}/{item.id}/{uuid4().hex}"
                # Persist the intended key before uploading so a killed worker can reclaim it.
                item.object_key = key
                db.commit()
                with output.open("rb") as body:
                    storage.put(key, body, "application/pdf" if item.kind == "document" else "image/webp")
                item.object_key, item.size_bytes = key, output.stat().st_size
                item.width, item.height = dimensions
                item.status, item.error, item.updated_at = "ready", None, now()
                if item.kind == "document":
                    cover = db.scalar(select(ResourcePreview).where(ResourcePreview.resource_id == row.id,
                        ResourcePreview.revision == item.revision, ResourcePreview.kind == "thumbnail"))
                    if cover is None:
                        cover = ResourcePreview(workspace_id=row.workspace_id, resource_id=row.id,
                            revision=item.revision, kind="thumbnail", status="ready")
                        db.add(cover); db.flush()
                    cover.object_key = key + ".cover"
                    with thumb.open("rb") as body:
                        storage.put(cover.object_key, body, "image/webp")
                    cover.size_bytes, cover.width, cover.height = thumb.stat().st_size, *dimensions
                    cover.status, cover.error, cover.accessed_at = "ready", None, now()
                db.commit()
        except Exception as error:
            db.rollback()
            if key:
                for failed_key in (key, key + ".cover"):
                    try:
                        storage.delete(failed_key)
                    except Exception:
                        logger.warning("Preview cache cleanup deferred")
            item = db.get(ResourcePreview, UUID(str(identifier)))
            item.status, item.error, item.updated_at = "failed", str(error)[:300] if isinstance(error, RuntimeError) else "预览准备失败，请重试或下载文件", now()
            db.commit()


def maintain(factory=SessionLocal):
    from .config import get_settings
    settings = get_settings()
    with factory() as db:
        storage = current_storage(db, require_active=False)
        if not storage:
            return
        items = list(db.scalars(select(ResourcePreview).order_by(ResourcePreview.accessed_at, ResourcePreview.id)))
        total = sum(item.size_bytes for item in items if item.status == "ready")
        for item in items:
            resource = db.get(WorkspaceFile, item.resource_id)
            stale = not resource or resource.deleted_at
            if item.status in {"queued", "running"} and aware(item.updated_at) < now()-timedelta(minutes=5):
                if item.object_key:
                    storage.delete(item.object_key)
                    storage.delete(item.object_key + ".cover")
                    item.object_key, item.size_bytes = None, 0
                item.status, item.updated_at = "queued", now()
                db.commit(); dispatch(item.id)
            if item.object_key and (stale or item.status == "failed" or aware(item.accessed_at) < now()-timedelta(seconds=settings.preview_cache_idle_seconds)
                    or total > settings.preview_cache_max_bytes):
                storage.delete(item.object_key)
                if item.status != "ready":
                    storage.delete(item.object_key + ".cover")
                if item.status == "ready":
                    total -= item.size_bytes
                item.object_key, item.size_bytes, item.status = None, 0, "expired"
        db.commit()
