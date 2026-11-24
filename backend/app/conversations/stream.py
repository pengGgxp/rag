"""将实时回答与持久化回答事件投递给 SSE 订阅者。"""
import asyncio
import json

import anyio
from sqlalchemy import select

from app.integrations.mcp_client import decrypt_headers
from ..models import ChatMessage, ContextOperation, ConversationEvent, ConversationExecution
from .messages import live_conversation, message_read


async def events(execution, stopped):
    loop = asyncio.get_running_loop()
    queue = asyncio.Queue()
    sentinel = object()

    def produce():
        try:
            for item in execution:
                loop.call_soon_threadsafe(queue.put_nowait, item)
        except BaseException as exc:
            loop.call_soon_threadsafe(queue.put_nowait, exc)
        finally:
            loop.call_soon_threadsafe(queue.put_nowait, sentinel)

    task = asyncio.create_task(asyncio.to_thread(produce))
    try:
        while True:
            try:
                item = await asyncio.wait_for(queue.get(), timeout=10)
            except TimeoutError:
                yield "heartbeat", {}
                continue
            if item is sentinel:
                break
            if isinstance(item, BaseException):
                raise item
            yield item
    finally:
        stopped.set()
        # 生产者记录当前 HTTP 调用的用量后再响应停止信号。
        with anyio.CancelScope(shield=True):
            await asyncio.shield(task)


def event(name, data):
    return {"event": name, "data": json.dumps(data, ensure_ascii=False)}


def event_text(payload):
    prefix = f"id: {payload['id']}\n" if payload.get("id") else ""
    return f"{prefix}event: {payload['event']}\ndata: {payload['data']}\n\n"


async def subscribe(factory, access, conversation_id, request_id, after=0):
    while True:
        with factory() as db:
            live_conversation(db, conversation_id, access.revalidate(db))
            row = db.get(ConversationExecution, request_id)
            if row is None or row.conversation_id != conversation_id:
                return
            events = list(db.scalars(select(ConversationEvent).where(ConversationEvent.request_id == request_id,
                ConversationEvent.sequence > after).order_by(ConversationEvent.sequence).limit(200)))
            status = row.status
            payloads = [(item.sequence, json.loads(decrypt_headers(item.encrypted_payload)["event"])) for item in events]
        newest = payloads[-1][0] if payloads else after
        for sequence, payload in payloads:
            after = sequence
            if "\nevent: model_recovering\n" in payload and (
                    sequence != newest or status not in ("queued", "running")):
                continue
            yield payload
        if status not in ("queued", "running") and len(payloads) < 200:
            if not any("\nevent: done\n" in payload for _, payload in payloads):
                # 恢复流程可能在原生产者写入结束事件之前完成。
                with factory() as db:
                    live_conversation(db, conversation_id, access.revalidate(db))
                    row = db.get(ConversationExecution, request_id)
                    message = db.get(ChatMessage, row.message_id) if row.message_id else None
                    if message and message.request_id != request_id:
                        message = None
                    operation = db.get(ContextOperation, request_id) if not row.message_id else None
                    data = message_read(message) if message else operation.result if operation else {"status": status}
                    yield event_text(event("done", {**data, "request_id": str(request_id)}))
            return
        yield ": heartbeat\n\n"
        await asyncio.sleep(0.3)
