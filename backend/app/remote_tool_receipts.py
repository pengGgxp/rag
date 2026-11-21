"""Durable admission by invocation identity, separate from argument equality."""
import json
from hashlib import sha256
from uuid import UUID, uuid5

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from . import mcp_client
from .models import ChatMessage, Conversation, RemoteToolExecution


class ReceiptError(RuntimeError):
    def __init__(self, message, *, uncertain=False):
        super().__init__(message)
        self.uncertain = uncertain


def invocation_id(request_id, call_id, agent_tool_execution_id=None):
    try:
        if agent_tool_execution_id is not None:
            return UUID(str(agent_tool_execution_id))
        if not request_id or not isinstance(call_id, str) or not call_id:
            raise ValueError
        return uuid5(UUID(str(request_id)), "remote-tool:" + call_id)
    except (ValueError, TypeError, AttributeError):
        raise ReceiptError("工具调用缺少稳定标识，本次操作未执行") from None


def claim(factory, access, message_id, request_id, identifier, name, signature, version, *, readonly=False):
    """Commit intent before IO; no database lock is held over the remote call."""
    fingerprint = sha256(signature.encode()).hexdigest()
    with factory() as db:
        checked = access.revalidate(db)
        if not message_id:
            raise ReceiptError("工具调用缺少会话记录，本次操作未执行")
        message = db.get(ChatMessage, message_id, with_for_update=True)
        if message is None:
            raise ReceiptError("工具调用所属会话不存在，本次操作未执行")
        checked.conversation(db, db.get(Conversation, message.conversation_id))
        if not request_id or message.request_id != request_id or message.role != "assistant":
            raise ReceiptError("工具调用与回答记录不匹配，本次操作未执行")
        previous = db.scalar(select(RemoteToolExecution).where(
            RemoteToolExecution.invocation_id == identifier))
        if previous:
            if (previous.account_id != access.user_id or previous.owner_id != access.user_id
                    or previous.message_id != message_id or previous.request_id != request_id
                    or previous.tool_name != name or previous.arguments_digest != fingerprint):
                raise ReceiptError("同一工具调用的身份或参数发生冲突，本次操作未执行")
            if previous.authorization_version != version:
                raise ReceiptError("历史操作授权已变化，请先核对原操作")
            if previous.encrypted_result:
                return previous.id, json.loads(mcp_client.decrypt_headers(previous.encrypted_result)["tool_result"])
            raise ReceiptError("此前操作结果尚未确认；未自动重试，请核对远端执行状态", uncertain=True)
        if not readonly:
            pending = db.scalar(select(RemoteToolExecution.id).where(
                RemoteToolExecution.message_id == message_id, RemoteToolExecution.owner_id == access.user_id,
                RemoteToolExecution.account_id == access.user_id,
                RemoteToolExecution.tool_name == name, RemoteToolExecution.arguments_digest == fingerprint,
                RemoteToolExecution.status.in_(("running", "uncertain")),
                RemoteToolExecution.encrypted_result.is_(None)).limit(1))
            if pending:
                raise ReceiptError("相同操作的结果尚未确认；未自动重试，请先核对原操作", uncertain=True)
        mcp_client.encrypt_headers({"tool_result": "{}"})
        row = RemoteToolExecution(invocation_id=identifier, message_id=message_id,
            account_id=access.user_id, owner_id=access.user_id, request_id=request_id,
            tool_name=name, authorization_version=version, arguments_digest=fingerprint, status="running")
        db.add(row)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            raise ReceiptError("该调用正在处理；未自动重试，请核对已有结果", uncertain=True) from None
        return row.id, None


def finish(factory, identifier, result=None, *, status=None):
    with factory() as db:
        row = db.get(RemoteToolExecution, identifier, with_for_update=True)
        if row is None:
            raise ReceiptError("操作回执无法保存，请先核对已有结果", uncertain=True)
        row.status = status or ("failed" if result.get("error") or result.get("isError") else "completed")
        if result is not None:
            row.encrypted_result = mcp_client.encrypt_headers(
                {"tool_result": json.dumps(result, ensure_ascii=True)})
        db.commit()
