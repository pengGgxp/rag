"""Authenticated downloads: reserve once, checkpoint actual ASGI body sends, release on disconnect."""
from urllib.parse import quote

from fastapi import HTTPException
from fastapi.responses import Response, StreamingResponse
from sqlalchemy.orm import sessionmaker
from starlette.concurrency import run_in_threadpool

from . import storage_billing as billing
from .storage import download_range_fileobj


def byte_range(value, size):
    if not value:
        return 0, size - 1, False
    try:
        if not value.startswith("bytes=") or "," in value:
            raise ValueError()
        first, last = value[6:].split("-", 1)
        if not first:
            length = int(last)
            if length <= 0:
                raise ValueError()
            start, end = max(0, size-length), size-1
        else:
            start, end = int(first), min(int(last), size-1) if last else size-1
        if start < 0 or start >= size or end < start:
            raise ValueError()
        return start, end, True
    except (ValueError, TypeError):
        raise HTTPException(416, "下载范围无效，请重新下载", headers={"Content-Range": f"bytes */{size}"}) from None


class MeteredResponse(StreamingResponse):
    def __init__(self, source, size, transfer_id, sessions, **kwargs):
        self.source, self.transfer_id, self.sessions = source, transfer_id, sessions
        def chunks():
            remaining = size
            while remaining:
                block = source.read(min(1024*1024, remaining))
                if not block:
                    raise IOError("Stored file is shorter than its metadata")
                remaining -= len(block)
                yield block
        super().__init__(chunks(), **kwargs)

    def checkpoint(self, **kwargs):
        with self.sessions() as db:
            billing.transfer_checkpoint(db, self.transfer_id, **kwargs)
            db.commit()

    async def __call__(self, scope, receive, send):
        import anyio
        completed = False
        async def heartbeat():
            while True:
                await anyio.sleep(30)
                await run_in_threadpool(self.checkpoint)
        async def metered_send(message):
            nonlocal completed
            length = len(message.get("body", b"")) if message["type"] == "http.response.body" else 0
            if length:
                await run_in_threadpool(self.checkpoint, preparing=length)
            await send(message)
            if length:
                await run_in_threadpool(self.checkpoint, sent=length)
            if message["type"] == "http.response.body" and not message.get("more_body", False):
                completed = True
        try:
            async with anyio.create_task_group() as group:
                group.start_soon(heartbeat)
                try:
                    await super().__call__(scope, receive, metered_send)
                finally:
                    group.cancel_scope.cancel()
        finally:
            # Shield cleanup from Starlette's disconnect cancellation scope.
            with anyio.CancelScope(shield=True):
                try:
                    await run_in_threadpool(self.checkpoint, finish="completed" if completed else "interrupted")
                finally:
                    self.source.close()


def download_response(db, access, request, *, object_key, size, filename, content_type, resource, inline=False,
                      source_factory=None):
    if size == 0 and request.headers.get("range"):
        raise HTTPException(416, "下载范围无效", headers={"Content-Range": "bytes */0"})
    start, end, partial = byte_range(request.headers.get("range"), size) if size else (0, -1, False)
    length = end-start+1
    headers = {"Accept-Ranges": "bytes", "Content-Length": str(length),
        "Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff",
        "Content-Disposition": f"{'inline' if inline else 'attachment'}; filename*=UTF-8''{quote(filename, safe='')}"}
    if partial:
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    if request.method == "HEAD":
        return Response(headers=headers, media_type=content_type, status_code=206 if partial else 200)
    identifier = billing.reserve_download(db, access, length, resource)
    db.commit()
    sessions = sessionmaker(bind=db.get_bind(), expire_on_commit=False)
    try:
        if source_factory:
            source = source_factory(object_key)
            source.seek(start)
        else:
            source = download_range_fileobj(object_key, start, end) if length else __import__("io").BytesIO()
    except BaseException:
        with sessions() as transaction:
            billing.transfer_checkpoint(transaction, identifier, finish="failed")
            transaction.commit()
        raise
    return MeteredResponse(source, length, identifier, sessions, media_type=content_type,
        headers=headers, status_code=206 if partial else 200)
