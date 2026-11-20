"""Ownership and cancellation for work executed inside a durable Agent turn."""
from datetime import timedelta

from fastapi import HTTPException
from sqlalchemy import select, update

from .auth import aware, now
from .models import AgentToolExecution, CloudComputerCommand, Conversation, ConversationExecution, ImageTask


def validate_owner(db, identifier, access, conversation_id=None, request_id=None):
    if identifier is None:
        return
    tool = db.get(AgentToolExecution, identifier)
    conversation = db.get(Conversation, tool.conversation_id) if tool else None
    if (not conversation or conversation.deleted_at
            or conversation.owner_id != access.user_id
            or conversation_id is not None and conversation.id != conversation_id
            or request_id is not None and tool.request_id != request_id):
        raise HTTPException(404, "工具执行记录不存在或无权访问")


def owner_active(db, identifier):
    tool = db.get(AgentToolExecution, identifier, populate_existing=True)
    conversation = db.get(Conversation, tool.conversation_id, populate_existing=True) if tool else None
    if not conversation or conversation.deleted_at or conversation.active_request_id != tool.request_id:
        return False
    execution = db.get(ConversationExecution, tool.request_id, populate_existing=True)
    return (tool.status == "running" and (execution is None or
        execution.status == "running" and execution.heartbeat_at is not None
        and aware(execution.heartbeat_at) > now() - timedelta(seconds=45)))


def cancel_request(db, request_id):
    """Only flag in-flight work; runners persist any already-submitted result."""
    tools = select(AgentToolExecution.id).where(AgentToolExecution.request_id == request_id)
    db.execute(update(ImageTask).where(ImageTask.agent_tool_execution_id.in_(tools),
        ImageTask.status.in_(("queued", "running", "saving"))).values(cancel_requested=True))
    db.execute(update(CloudComputerCommand).where(CloudComputerCommand.agent_tool_execution_id.in_(tools),
        CloudComputerCommand.status.in_(("queued", "preparing", "running"))).values(cancel_requested=True))
