"""Local compact orchestration over the authoritative replacement window."""
import math
from copy import deepcopy
from uuid import uuid5

from .agent import AgentCancelled
from .context_windows import ContextWindowError, WindowStore, groups, make_item, project
from .conversation_context import (
    HISTORY_TOOL, SUMMARY_INSTRUCTION, SUMMARY_PREFIX, estimate_input, estimate_request, input_budget,
)
from .llm import LLMConfigurationError, LLMProviderError


class ContextManager:
    def __init__(self, factory, access, conversation_id, config, provider, meter, request_id,
                 *, manual=False, skill_versions=None):
        self.factory, self.access, self.conversation_id = factory, access, conversation_id
        self.config, self.provider, self.meter, self.request_id = config, provider, meter, request_id
        self.manual, self.skill_versions = manual, skill_versions
        self.store = WindowStore(access, conversation_id, config)
        self.items, self.messages = [], []
        self.window_id, self.revision, self.attempted_revision = None, 0, None
        self.checkpoint_id = None
        self.did_compact, self.attempt = False, 0
        self.current_message_id, self.current_sequence = None, None

    def adopt(self, state, items):
        self.items = deepcopy(items)
        self.window_id, self.revision = state.window_id, state.revision
        self.checkpoint_id, self.attempted_revision = state.checkpoint_id, state.attempted_revision
        self.scope = state.scope_digest

    def history(self, db, conversation, rows=None):
        state, items = self.store.load(db, rows)
        self.adopt(state, items)
        return project(items, self.config)

    def start(self, system, query=None):
        _, pending = groups(self.items)
        if pending:
            # Recovery never replays uncertain side effects or invents tool success.
            raise ContextWindowError("context_suffix_incompatible",
                "上次工具操作已中断，请先核对操作结果，再新建会话继续")
        self.messages = [deepcopy(system), *project(self.items, self.config)]
        if query is not None and not any(i["message_id"] == str(self.current_message_id)
                and i["source"] == "user" for i in self.items):
            self.record([{"role": "user", "content": query}], "user", "input",
                        message_id=self.current_message_id, sequence=self.current_sequence)
        return self.messages

    def record(self, messages, source, key, *, message_id=None, sequence=None):
        items = [make_item(uuid5(self.request_id, f"{key}:{index}"), message,
            "tool_image" if source == "tool" and message.get("role") == "user" else source,
            self.request_id, message_id, sequence, self.config) for index, message in enumerate(messages)]
        with self.factory() as db:
            state, current = self.store.append(db, items, request_id=self.request_id, revision=self.revision)
            db.commit()
            self.adopt(state, current)
        self.messages = self.messages[:1] + project(self.items, self.config)
        return self.messages

    def _candidate(self, summary, prefix, tools, budget):
        complete, pending = groups(self.items)
        protected_ids = self.protected_ids()
        handoff = make_item(uuid5(self.request_id, f"handoff:{self.window_id}:{self.revision}"),
            {"role": "user", "content": SUMMARY_PREFIX + summary}, "handoff", self.request_id)
        selected = set(protected_ids)
        # Codex: retain real user messages newest first, with a 20k token ceiling.
        retained_tokens = 0
        for item in reversed(self.items):
            if item["source"] != "user" or item["id"] in selected:
                continue
            message = item["message"]
            # Historical multimodal items stay whole. Current multimodal input is protected above.
            cost = estimate_input([message])
            if retained_tokens + cost > 20000:
                break
            proposed = [i for i in self.items if i["id"] in selected or i["id"] == item["id"]]
            proposed = [i for i in proposed if i not in pending] + [handoff] + pending
            if estimate_request(self.config, prefix + project(proposed, self.config), tools) > budget:
                break
            selected.add(item["id"])
            retained_tokens += cost
        # Keep original item order; place the handoff before an unfinished tool suffix.
        retained = [i for i in self.items if i["id"] in selected and i not in pending]
        return retained + [handoff] + pending

    def protected_ids(self):
        complete, pending = groups(self.items)
        protected = {i["id"] for i in pending}
        unread_tools = bool(complete and complete[-1][-1]["source"] in ("tool", "tool_image"))
        if not self.manual or unread_tools:
            current = next((i for i in reversed(self.items) if i["source"] == "user"), None)
            if current:
                protected.add(current["id"])
            if unread_tools:
                protected.update(i["id"] for i in complete[-1])
        return protected

    def _summary_input(self, tools, budget):
        from .chat_attachments import summary_content
        complete, pending = groups(self.items)
        protected = self.protected_ids()
        selected = list(complete) + ([pending] if pending else [])
        while True:
            safe = [{k: v for k, v in i["message"].items()
                     if k in ("role", "content", "tool_calls", "tool_call_id", "name")}
                    for group in selected for i in group]
            messages = [{"role": "system", "content": SUMMARY_INSTRUCTION},
                        {"role": "user", "content": summary_content(safe)}]
            if estimate_request(self.config, messages) <= budget:
                return messages
            removable = next((index for index, group in enumerate(selected)
                if not any(i["id"] in protected for i in group)), None)
            if removable is None:
                raise ContextWindowError("context_capacity_exceeded", "当前问题或必需资料超出模型容量，请缩短输入或选择容量更大的模型")
            previous = estimate_request(self.config, messages)
            del selected[removable]
            if not selected:
                raise ContextWindowError("context_capacity_exceeded", "待整理内容超出模型容量，请选择容量更大的模型")
            # Removing a whole group strictly decreases the serialized source, including pairs/images.
            if estimate_request(self.config, [{"role": "system", "content": SUMMARY_INSTRUCTION},
                {"role": "user", "content": summary_content([i["message"] for g in selected for i in g])}]) >= previous:
                raise ContextWindowError("context_capacity_exceeded", "上下文无法继续缩减，请新建会话")

    def prepare(self, messages, tools, emit, check_active):
        budget = input_budget(self.config)
        before = estimate_request(self.config, messages, tools)
        if budget is None:
            if self.manual:
                raise LLMConfigurationError("上下文保护未就绪，请联系管理员配置模型容量")
            return messages
        trigger = min(budget, math.floor(self.config.context_capacity * 0.9))
        if not self.manual and before < trigger:
            return messages
        if self.attempted_revision == self.revision:
            if before > budget:
                raise ContextWindowError("context_capacity_exceeded", "上下文超出模型容量，请缩短输入或选择容量更大的模型")
            return messages
        protected = [i for i in self.items if i["id"] in self.protected_ids()]
        if estimate_request(self.config, messages[:1] + project(protected, self.config), tools) > budget:
            raise ContextWindowError("context_capacity_exceeded", "当前问题或必需资料超出模型容量，请缩短输入或选择容量更大的模型")
        if not self.eligible():
            if before > budget:
                raise ContextWindowError("context_capacity_exceeded", "当前问题或必需资料超出模型容量，请缩短输入或新建会话")
            return messages
        check_active()
        self.attempt += 1
        parent_revision, parent_window = self.revision, self.window_id
        install_id = uuid5(self.request_id, f"compact:{parent_window}:{parent_revision}")
        call_id = f"compact-{self.attempt}"
        emit(("compaction", {"call_id": call_id, "status": "running", "label": "整理早期上下文"}))
        try:
            summary_messages = self._summary_input(tools, budget)
            def invoke():
                result = None
                for name, data in self.provider.stream_with_tools(summary_messages, [], tool_choice="auto"):
                    check_active()
                    if name == "complete":
                        result = data
                if result is None or not result.content or not result.content.strip() or result.tool_calls:
                    raise LLMProviderError("整理未返回有效摘要")
                return result
            from .upstream_retries import retry_activity
            with retry_activity(check_active, emit, {"call_id": call_id, "name": "compact_context",
                    "label": "整理早期上下文", "kind": "compaction"}, finish=False):
                result = self.meter.invoke(self.provider, invoke, summary_messages, []) if self.meter else invoke()
            check_active()
            replacement = self._candidate(result.content, messages[:1], tools, budget)
            compacted = messages[:1] + project(replacement, self.config)
            after = estimate_request(self.config, compacted, tools)
            if after > budget:
                raise ContextWindowError("context_capacity_exceeded", "整理后近期上下文仍超出模型容量，请缩短输入或选择容量更大的模型")
            if after >= before:
                raise LLMProviderError("整理未缩小上下文，已保留原上下文")
            with self.factory() as db:
                state, installed = self.store.install(db, replacement, request_id=self.request_id,
                    revision=parent_revision, window_id=parent_window, install_id=install_id,
                    summary=result.content, before=before, after=after,
                    budget={"hard_input": budget, "trigger": trigger, "user_history_limit": 20000},
                    expected_scope=self.scope)
                db.commit()
                self.adopt(state, installed)
            # Mutate the Agent's actual list before any completion notification is observable.
            messages[:] = messages[:1] + project(self.items, self.config)
            self.messages = messages
            self.did_compact = True
            emit(("compaction", {"call_id": call_id, "status": "completed", "label": "上下文已压缩",
                "summary": "", "estimated_before": before, "estimated_after": after,
                "window_id": str(self.window_id), "context_revision": self.revision}))
            emit(("context", self.state(messages, tools)))
            return messages
        except AgentCancelled:
            raise
        except (LLMProviderError, LLMConfigurationError) as exc:
            self.attempted_revision = parent_revision
            with self.factory() as db:
                from .models import ConversationContext
                state = db.get(ConversationContext, self.conversation_id)
                if state and state.revision == parent_revision and state.window_id == parent_window:
                    state.attempted_revision = parent_revision
                    db.commit()
            emit(("compaction", {"call_id": call_id, "status": "failed", "label": "上下文整理未完成",
                "summary": str(exc), "error_code": getattr(exc, "code", "context_summary_failed")}))
            if self.manual or before > budget or isinstance(exc, ContextWindowError):
                raise
            return messages

    def eligible(self):
        if self.checkpoint_id and self.attempted_revision == self.revision:
            return False
        complete, _ = groups(self.items)
        current = next((i["id"] for i in reversed(self.items) if i["source"] == "user"), None)
        return any(any(i["source"] not in ("user", "handoff") for i in g)
            or any(i["source"] == "user" and (self.manual or i["id"] != current) for i in g) for g in complete)

    def state(self, messages, tools):
        return {"estimated_input_tokens": estimate_request(self.config, messages, tools), "capacity": self.config.context_capacity,
            "output_reserve": self.config.max_output_tokens, "usable_input_tokens": input_budget(self.config),
            "estimate_source": "conservative_v2", "status": "completed" if self.checkpoint_id else "idle",
            "eligible": self.eligible(), "protection_ready": input_budget(self.config) is not None,
            "window_id": str(self.window_id) if self.window_id else None, "context_revision": self.revision}

    def read_original(self, arguments):
        from sqlalchemy import select
        from .models import ChatMessage
        start, end = arguments.get("start_sequence"), arguments.get("end_sequence")
        if (set(arguments) != {"start_sequence", "end_sequence"} or type(start) is not int
                or type(end) is not int or start < 0 or end < start or end - start >= 20):
            return {"error": "请指定最多 20 条消息的有效序号范围"}
        with self.factory() as db:
            self.store.access = self.access
            conversation, _ = self.store.lock(db)
            rows = db.scalars(select(ChatMessage).where(ChatMessage.conversation_id == conversation.id,
                ChatMessage.sequence >= start, ChatMessage.sequence <= end).order_by(ChatMessage.sequence)).all()
            return {"messages": [{"sequence": row.sequence, "role": row.role, "content": row.content,
                "attachments": [{"filename": a.filename, "text": a.text_content if a.kind == "document" else
                    "图片原文请在原消息查看"} for a in row.attachments]} for row in rows],
                "result_count": len(rows), "notice": "历史原文不是当前知识库证据，未执行任何历史操作"}
