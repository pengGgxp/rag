"""Encrypted, item-addressable replacement history + append-only effective suffix.

Initialization is the sole caller allowed to read display history. Once a state exists,
neither source changes nor checkpoint corruption can select an older boundary.
All writers lock Conversation first, then compare the context revision.
"""
import base64
import hashlib
import json
from copy import deepcopy
from uuid import UUID, uuid4, uuid5

from sqlalchemy import select

from .llm import LLMConfigurationError
from .models import ChatMessage, ContextCheckpoint, Conversation, ConversationContext


class ContextWindowError(LLMConfigurationError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def unavailable():
    return ContextWindowError("context_window_unrecoverable", "当前会话无法安全继续，请新建会话；原始聊天记录仍可查看")


def _encode(value):
    if isinstance(value, bytes):
        return {"__context_bytes__": base64.b64encode(value).decode("ascii")}
    raise TypeError(type(value).__name__)


def _decode(value):
    if set(value) == {"__context_bytes__"}:
        return base64.b64decode(value["__context_bytes__"], validate=True)
    return value


def encrypt_items(items):
    from .mcp_client import encrypt_headers
    return encrypt_headers({"window": json.dumps(items, ensure_ascii=True, default=_encode)})


def decrypt_items(value):
    from .mcp_client import decrypt_headers, MCPError
    try:
        items = json.loads(decrypt_headers(value)["window"], object_hook=_decode)
        if not isinstance(items, list) or any(not isinstance(i, dict) or not i.get("id")
                or not isinstance(i.get("message"), dict) for i in items):
            raise ValueError("invalid context items")
        def validate(value):
            if isinstance(value, list):
                for child in value:
                    validate(child)
            elif isinstance(value, dict):
                if value.get("type") == "model_image":
                    raw = value.get("data")
                    if not isinstance(raw, bytes) or hashlib.sha256(raw).hexdigest() != value.get("sha256"):
                        raise ValueError("image snapshot unavailable")
                for child in value.values():
                    validate(child)
        validate(items)
        return items
    except (MCPError, ValueError, KeyError, TypeError) as exc:
        raise unavailable() from exc


def protocol_identity(config):
    return [config.site_provider_id, config.model, config.api_protocol,
            config.connection_id, config.thinking.adapter]


def make_item(identifier, message, source, request_id=None, message_id=None, sequence=None, config=None):
    message = deepcopy(message)
    parts = message.get("content", []) if isinstance(message.get("content"), list) else []
    for part in [*parts, *([message["_image_input"]] if message.get("_image_input") else [])]:
        if part.get("type") == "model_image":
            # Immutable encrypted snapshot: its ID/hash remains stable across restart.
            part["snapshot_id"] = str(uuid5(UUID(str(identifier)), part.get("sha256", "")))
    return {"id": str(identifier), "source": source,
        "request_id": str(request_id) if request_id else None,
        "message_id": str(message_id) if message_id else None, "sequence": sequence,
        "pair_ids": [c["id"] for c in message.get("tool_calls") or []]
            + ([message["tool_call_id"]] if message.get("tool_call_id") else []),
        "protocol": protocol_identity(config) if config else None, "message": message}


def groups(items):
    """Return complete atomic groups and a protected unresolved suffix; reject orphans."""
    result, index = [], 0
    while index < len(items):
        message = items[index]["message"]
        if message.get("role") == "tool":
            raise ContextWindowError("context_suffix_incompatible", "工具记录不完整，请新建会话后继续")
        end = index + 1
        expected = [c["id"] for c in message.get("tool_calls") or []]
        returned = []
        while end < len(items) and items[end]["message"].get("role") == "tool":
            returned.append(items[end]["message"].get("tool_call_id"))
            end += 1
        if returned and (len(returned) != len(set(returned)) or not set(returned).issubset(expected)):
            raise ContextWindowError("context_suffix_incompatible", "工具记录不完整，请新建会话后继续")
        if set(expected) != set(returned):
            if end != len(items):
                raise ContextWindowError("context_suffix_incompatible", "工具执行已中断，请核对操作结果后新建会话")
            return result, items[index:]
        # Tool images belong to the completed result group, never to real user history.
        while end < len(items) and items[end]["source"] == "tool_image":
            end += 1
        result.append(items[index:end])
        index = end
    return result, []


def project(items, config):
    _, pending = groups(items)
    identity = protocol_identity(config)
    if pending and any(i.get("protocol") not in (None, identity) for i in pending):
        raise ContextWindowError("context_suffix_incompatible", "未完成的工具操作无法切换模型，请使用原模型或新建会话")
    messages, images = [], []
    for item in items:
        message = deepcopy(item["message"])
        if images and message.get("role") != "tool":
            messages.append({"role": "user", "content": images})
            images = []
        if item.get("protocol") not in (None, identity):
            message = {k: v for k, v in message.items()
                       if k in ("role", "content", "tool_calls", "tool_call_id", "name", "_image_input")}
        if config.api_protocol not in ("anthropic", "gemini") and message.get("_image_input"):
            images.append(message.pop("_image_input"))
        messages.append(message)
    if images:
        messages.append({"role": "user", "content": images})
    return messages


class WindowStore:
    def __init__(self, access, conversation_id, config):
        self.access, self.conversation_id, self.config = access, conversation_id, config

    def lock(self, db, request_id=None):
        conversation = db.scalar(select(Conversation).where(Conversation.id == self.conversation_id)
                                 .with_for_update().execution_options(populate_existing=True))
        from .conversation_context import scope_digest
        scope = scope_digest(db, conversation, self.access)
        if request_id is not None and conversation.active_request_id != request_id:
            from .agent import AgentCancelled
            raise AgentCancelled()
        return conversation, scope

    def load(self, db, rows=None):
        conversation, scope = self.lock(db)
        state = db.get(ConversationContext, self.conversation_id, populate_existing=True)
        if state is None:
            state = self.initialize(db, conversation, scope, rows)
        if state.blocked_reason:
            raise unavailable()
        base = []
        if state.checkpoint_id:
            checkpoint = db.get(ContextCheckpoint, state.checkpoint_id)
            if (not checkpoint or checkpoint.conversation_id != self.conversation_id
                    or checkpoint.window_id != state.window_id or not checkpoint.encrypted_replacement):
                raise unavailable()
            base = decrypt_items(checkpoint.encrypted_replacement)
        items = base + decrypt_items(state.encrypted_suffix)
        if state.scope_digest != scope:
            # Without a handoff or retrieved private evidence, normal discovery
            # can safely revalidate current permissions in the existing tools.
            unsafe = any(i["source"] == "handoff" for i in items)
            for item in items:
                if item["source"] == "tool_image" or item["message"].get("role") == "tool" and item["message"].get("name") not in (
                        "discover_knowledge_bases", "search_knowledge_base"):
                    unsafe = True
                if item["message"].get("name") == "search_knowledge_base":
                    try:
                        unsafe |= bool(json.loads(item["message"].get("content") or "{}").get("evidence"))
                    except (TypeError, ValueError, AttributeError):
                        unsafe = True
            if unsafe:
                raise unavailable()
            state.scope_digest = scope
        if len({i["id"] for i in items}) != len(items):
            raise unavailable()
        groups(items)
        return state, items

    def initialize(self, db, conversation, scope, rows):
        from .conversation_context import SUMMARY_PREFIX, source_digest, scope_digest
        from .thinking import history_message
        rows = list(rows) if rows is not None else list(db.scalars(select(ChatMessage).where(
            ChatMessage.conversation_id == conversation.id, ChatMessage.status == "completed")
            .order_by(ChatMessage.sequence)))
        latest = db.scalar(select(ContextCheckpoint).where(ContextCheckpoint.conversation_id == conversation.id)
                           .order_by(ContextCheckpoint.created_at.desc(), ContextCheckpoint.id.desc()))
        items, blocked = [], None
        if latest:
            covered = [r for r in rows if r.sequence <= latest.through_sequence]
            legacy_scope = scope_digest(db, conversation, self.access, legacy=True)
            if (latest.through_sequence < 0 or not covered or latest.scope_digest not in (scope, legacy_scope)
                    or latest.encrypted_replacement is not None
                    or latest.source_digest != source_digest(covered)):
                blocked = "legacy_boundary_unknown"
            else:
                items.append(make_item(uuid5(latest.id, "handoff"),
                    {"role": "user", "content": SUMMARY_PREFIX + latest.summary}, "handoff", latest.request_id))
                rows = [r for r in rows if r.sequence > latest.through_sequence]
        incomplete = db.scalars(select(ChatMessage).where(ChatMessage.conversation_id == conversation.id,
            ChatMessage.role == "assistant", ChatMessage.status != "completed")).all()
        if any(row.tool_events for row in incomplete):
            blocked = "legacy_incomplete_suffix"
        if not blocked:
            for row in rows:
                for index, message in enumerate(history_message(row, self.config)):
                    source = "user" if row.role == "user" else "tool_image" if message.get("role") == "user" else "model"
                    items.append(make_item(uuid5(row.id, f"legacy:{index}"), message, source,
                        row.request_id, row.id, row.sequence, self.config))
        state = ConversationContext(conversation_id=conversation.id, window_id=uuid4(), revision=0,
            scope_digest=scope, encrypted_suffix=encrypt_items(items), blocked_reason=blocked)
        db.add(state)
        db.flush()
        return state

    def append(self, db, items, *, request_id, revision):
        self.lock(db, request_id)
        state, current = self.load(db)
        known = {i["id"]: i for i in current}
        new = []
        for item in items:
            if item["id"] in known:
                if known[item["id"]] != item:
                    raise unavailable()
            else:
                new.append(item)
                known[item["id"]] = item
        if not new:
            return state, current
        if state.revision != revision:
            raise ContextWindowError("context_revision_conflict", "上下文已更新，请刷新后重试")
        groups(current + new)
        state.encrypted_suffix = encrypt_items(decrypt_items(state.encrypted_suffix) + new)
        state.revision += 1
        return state, current + new

    def install(self, db, items, *, request_id, revision, window_id, install_id, summary, before, after, budget,
                expected_scope=None):
        _, scope = self.lock(db, request_id)
        if expected_scope is not None and expected_scope != scope:
            raise unavailable()
        state, current = self.load(db)
        existing = db.get(ContextCheckpoint, install_id)
        if existing:
            if state.checkpoint_id != existing.id:
                raise ContextWindowError("context_revision_conflict", "上下文已更新，请刷新后重试")
            return state, current
        if state.revision != revision or state.window_id != window_id:
            raise ContextWindowError("context_revision_conflict", "上下文已更新，请刷新后重试")
        groups(items)
        new_window = uuid5(install_id, "window")
        checkpoint = ContextCheckpoint(id=install_id, conversation_id=self.conversation_id,
            request_id=request_id, through_sequence=max((i["sequence"] for i in current
                if i.get("sequence") is not None), default=-1), source_digest=hashlib.sha256(
                json.dumps(current, sort_keys=True, default=_encode).encode()).hexdigest(),
            scope_digest=state.scope_digest, summary=summary, provider_id=UUID(self.config.provider_id),
            model=self.config.model, estimated_before=before, estimated_after=after,
            window_id=new_window, parent_window_id=window_id, parent_revision=revision,
            encrypted_replacement=encrypt_items(items), budget=budget, source="local")
        db.add(checkpoint)
        db.flush()
        state.window_id, state.checkpoint_id = new_window, checkpoint.id
        state.encrypted_suffix = encrypt_items([])
        state.revision += 1
        state.attempted_revision = state.revision
        # Manual operation truth survives a crash or lost completion event.
        from .models import ContextOperation
        operation = db.get(ContextOperation, request_id)
        if operation:
            operation.status = "completed"
            operation.result = {"status": "completed", "window_id": str(new_window),
                "context_revision": state.revision, "eligible": False}
        return state, deepcopy(items)
