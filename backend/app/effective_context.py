"""Local compact orchestration over the authoritative replacement window."""
import math
import json
from copy import deepcopy
from uuid import UUID, uuid5

from .agent import AgentCancelled
from .context_windows import ContextWindowError, WindowStore, groups, make_item, project
from .conversation_context import (
    HISTORY_TOOL, SUMMARY_INSTRUCTION, SUMMARY_PREFIX, estimate_input, estimate_request, input_budget,
)
from .llm import LLMConfigurationError, LLMProviderError, LLMContextCapacityError


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
        complete, _ = groups(items)
        # Pending calls are normalized in start, after the new request owns the
        # generation slot. Previewing context must not replay or finalize them.
        return project([i for group in complete for i in group], self.config)

    def start(self, system, query=None):
        _, pending = groups(self.items)
        if pending:
            self.recover_tools(pending)
        self.archive_large_results()
        self.messages = [deepcopy(system), *project(self.bounded_items(), self.config)]
        if query is not None and not any(i["message_id"] == str(self.current_message_id)
                and i["source"] == "user" for i in self.items):
            self.record([{"role": "user", "content": query}], "user", "input",
                        message_id=self.current_message_id, sequence=self.current_sequence)
        return self.messages

    def record(self, messages, source, key, *, message_id=None, sequence=None, plan=None, before_append=None):
        items = [make_item(uuid5(self.request_id, f"{key}:{index}"), message,
            "tool_image" if source == "tool" and message.get("role") == "user" else source,
            self.request_id, message_id, sequence, self.config) for index, message in enumerate(messages)]
        for item in items:
            if plan:
                item["plan"] = deepcopy(plan)
            if item["message"].get("tool_calls"):
                item["tool_intent_version"] = 1
                item["model_key"] = key
        with self.factory() as db:
            if before_append is not None and not before_append(db):
                return None
            state, current = self.store.append(db, items, request_id=self.request_id, revision=self.revision)
            db.commit()
            self.adopt(state, current)
        self.messages = self.messages[:1] + project(self.bounded_items(), self.config)
        return self.messages

    def steer(self, entry):
        from .conversation_jobs import mark_steered
        return self.record([{"role": "user", "content": entry["text"]}], "user", f"steer:{entry['id']}",
            message_id=entry["id"], before_append=lambda db: mark_steered(db, self.conversation_id, self.request_id, entry))

    def output_budget(self, calls=1):
        return max(300, min(4000, (input_budget(self.config) or 24000) // 4 // max(1, calls)))

    def result_projections(self):
        from .agent_tools import bounded_message
        complete, pending = groups(self.items)
        for group in [*complete, *([pending] if pending else [])]:
            model = group[0]
            for item in group[1:]:
                if item["message"].get("role") == "tool":
                    identifier = uuid5(UUID(model["id"]), item["message"]["tool_call_id"])
                    visible = bounded_message(item["message"], identifier, self.output_budget(len(model["pair_ids"])))
                    yield item, identifier, visible, UUID(model["id"])

    def bounded_items(self):
        replacements = {item["id"]: visible for item, _, visible, _ in self.result_projections()}
        return [{**item, "message": replacements.get(item["id"], item["message"])} for item in self.items]

    def archive_large_results(self):
        """Give legacy oversized observations the same readable receipt as new calls."""
        from .context_windows import encrypt_items
        from .models import AgentToolExecution
        oversized = [(item, identifier, model_id) for item, identifier, visible, model_id in self.result_projections()
            if visible != item["message"]]
        if not oversized:
            return
        with self.factory() as db:
            self.store.lock(db, self.request_id)
            for item, identifier, model_id in oversized:
                if db.get(AgentToolExecution, identifier) is None:
                    db.add(AgentToolExecution(id=identifier, conversation_id=self.conversation_id,
                        request_id=UUID(item["request_id"]) if item.get("request_id") else self.request_id,
                        model_item_id=model_id,
                        call_id=item["message"]["tool_call_id"], name=item["message"].get("name", ""),
                        status="completed", encrypted_result=encrypt_items([item])))
            db.commit()

    def begin_tool(self, call):
        from .models import AgentToolExecution
        from .mcp_client import encrypt_headers
        _, pending = groups(self.items)
        if not pending or call.id not in pending[0]["pair_ids"]:
            raise ContextWindowError("context_suffix_incompatible", "工具调用缺少对应模型记录")
        model_item = UUID(pending[0]["id"])
        identifier = uuid5(model_item, call.id)
        with self.factory() as db:
            self.store.lock(db, self.request_id)
            if db.get(AgentToolExecution, identifier):
                # This slot was already admitted. Never execute its side effect twice.
                raise ContextWindowError("tool_execution_already_started", "该工具调用已经开始，请继续会话核对已有结果")
            db.add(AgentToolExecution(id=identifier, conversation_id=self.conversation_id,
                request_id=self.request_id, model_item_id=model_item, call_id=call.id, name=call.name, status="running",
                encrypted_arguments=encrypt_headers({"arguments": json.dumps(call.arguments)})))
            db.commit()
        return identifier

    def finish_tool(self, identifier, message, budget):
        from .context_windows import encrypt_items
        from .models import AgentToolExecution
        from .agent_tools import bounded_message
        raw = make_item(uuid5(identifier, "full-result"), message, "tool", self.request_id)
        with self.factory() as db:
            # Save known truth even if stop changed active_request_id while the
            # tool was finishing. Only window appends require the active slot.
            row = db.get(AgentToolExecution, identifier, with_for_update=True)
            row.encrypted_result = encrypt_items([raw])
            data = json.loads(message["content"])
            row.status = ("uncertain" if data.get("uncertain") else "cancelled" if data.get("status") == "cancelled"
                else "failed" if data.get("error") or data.get("isError") else "completed")
            db.commit()
        visible = bounded_message(message, identifier, budget)
        self.record([visible], "tool", f"tool-result:{identifier}",
            plan=data if message.get("name") == "update_plan" and data.get("plan") and not data.get("error") else None)
        return visible

    def recover_tools(self, pending):
        from .context_windows import decrypt_items, encrypt_items
        from .models import AgentToolExecution
        model = pending[0]
        returned = {i["message"].get("tool_call_id") for i in pending[1:]}
        missing = [c for c in model["message"]["tool_calls"] if c["id"] not in returned]
        recovered, plan = [], None
        for call in missing:
            identifier = uuid5(UUID(model["id"]), call["id"])
            with self.factory() as db:
                self.store.lock(db, self.request_id)
                row = db.get(AgentToolExecution, identifier)
                if row and row.encrypted_result:
                    message = decrypt_items(row.encrypted_result)[0]["message"]
                else:
                    result = self._business_receipt(db, model, call)
                    if result is None:
                        not_started = model.get("tool_intent_version") == 1 and row is None
                        result = {"error": "上次调用尚未执行" if not_started else "上次调用已中断，结果尚未确认；请核对已有结果，勿盲目重复操作",
                            "execution_status": "not_executed" if not_started else "uncertain", "uncertain": not not_started}
                    message = {"role": "tool", "tool_call_id": call["id"], "name": call["function"]["name"],
                               "content": json.dumps(result, ensure_ascii=False)}
                    if row is None:
                        row = AgentToolExecution(id=identifier, conversation_id=self.conversation_id,
                            request_id=UUID(model["request_id"]) if model.get("request_id") else self.request_id,
                            model_item_id=UUID(model["id"]), call_id=call["id"], name=call["function"]["name"])
                        db.add(row)
                    row.status = result.get("execution_status") or ("uncertain" if result.get("uncertain") else
                        "cancelled" if result.get("status") == "cancelled" else
                        "failed" if result.get("error") or result.get("isError") else "completed")
                    row.encrypted_result = encrypt_items([make_item(uuid5(identifier, "recovered-result"),
                        message, "tool", self.request_id)])
                db.commit()
            from .agent_tools import bounded_message
            if message.get("name") == "update_plan":
                value = json.loads(message["content"])
                if value.get("plan") and not value.get("error"):
                    plan = value
            recovered.append(bounded_message(message, identifier, self.output_budget(len(missing))))
        # Close the whole outstanding suffix before projecting to another
        # protocol. No intermediate incomplete group is sent to any model.
        self.record(recovered, "tool", f"recovered:{model['id']}", plan=plan)

    def _business_receipt(self, db, model, call):
        """Read existing local receipts only; never poll/replay an external tool."""
        from sqlalchemy import select
        from hashlib import sha256
        from .models import KnowledgeBaseToolExecution, RemoteToolExecution
        from .mcp_client import decrypt_headers
        if not model.get("request_id"):
            return None
        name, request = call["function"]["name"], UUID(model["request_id"])
        if name in ("generate_image", "edit_image"):
            from .models import ImageTask
            from .image_tasks import TERMINAL, tool_read
            task = db.scalar(select(ImageTask).where(
                ImageTask.agent_tool_execution_id == uuid5(UUID(model["id"]), call["id"]),
                ImageTask.workspace_id == self.access.workspace_id,
                ImageTask.actor_user_id == self.access.user_id))
            if task and task.status in TERMINAL:
                return tool_read(task)
        key = model.get("model_key", "")
        if key.startswith("model:"):
            activity = f"tool-{key.split(':')[1]}-{call['id']}"
            receipt = db.get(KnowledgeBaseToolExecution, uuid5(request, "knowledge-base-tool:" + activity))
            if receipt and receipt.workspace_id == self.access.workspace_id and receipt.user_id == self.access.user_id:
                return receipt.result
            from .models import CloudComputerCommand, CloudComputerSession
            from .cloud_computer import tool_request_id
            command = db.scalar(select(CloudComputerCommand).join(CloudComputerSession).where(
                CloudComputerCommand.request_id == tool_request_id(request, activity),
                CloudComputerSession.conversation_id == self.conversation_id,
                CloudComputerSession.workspace_id == self.access.workspace_id,
                CloudComputerSession.user_id == self.access.user_id))
            if command and command.status not in ("queued", "preparing", "running", "submitted") and command.result:
                result = dict(command.result)
                if name in ("write_file", "apply_patch", "delete_file", "read_file", "list_files"):
                    file_result = result.get("file_result", {})
                    return ({"error": file_result["error"], "status_code": file_result.get("status_code")}
                        if file_result.get("error") else file_result.get("value") or result)
                return result
        from .question_workbench import LABELS as WORKBENCH_NAMES
        if name.startswith("mcp_") or name in WORKBENCH_NAMES:
            try:
                args = json.loads(call["function"]["arguments"])
            except (TypeError, ValueError):
                return None
            signature = json.dumps([name, args], sort_keys=True, ensure_ascii=True)
            identifier = uuid5(UUID(model["id"]), call["id"])
            fingerprint = sha256(signature.encode()).hexdigest()
            scope = (
                RemoteToolExecution.request_id == request,
                RemoteToolExecution.owner_id == self.access.user_id, RemoteToolExecution.workspace_id == self.access.workspace_id,
                RemoteToolExecution.tool_name == name, RemoteToolExecution.arguments_digest == fingerprint)
            receipt = db.scalar(select(RemoteToolExecution).where(*scope,
                RemoteToolExecution.invocation_id == identifier))
            if receipt is None:
                from .models import AgentToolExecution
                executions = db.scalars(select(AgentToolExecution.id).where(
                    AgentToolExecution.request_id == request, AgentToolExecution.name == name)).all()
                legacy = db.scalars(select(RemoteToolExecution).where(*scope,
                    RemoteToolExecution.invocation_id.is_(None))).all()
                # Parameter equality alone never identifies an invocation.
                if executions == [identifier] and len(legacy) == 1:
                    receipt = legacy[0]
            if receipt and receipt.encrypted_result:
                return json.loads(decrypt_headers(receipt.encrypted_result)["tool_result"])
        return None

    def read_result(self, arguments):
        from .context_windows import decrypt_items
        from .models import AgentToolExecution
        try:
            identifier = UUID(arguments["result_id"])
            offset, limit = arguments.get("offset", 0), arguments.get("limit", 4000)
            if (set(arguments) - {"result_id", "offset", "limit", "field", "find", "candidate_offset"}
                    or type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 8000
                    or ("field" in arguments and not isinstance(arguments["field"], str))
                    or ("find" in arguments and (not isinstance(arguments["find"], str) or not arguments["find"]))
                    or ("candidate_offset" in arguments and (type(arguments["candidate_offset"]) is not int
                        or arguments["candidate_offset"] < 0))):
                raise ValueError
        except (ValueError, KeyError, TypeError):
            return {"error": "请提供有效的 result_id、offset 和 1 至 8000 的 limit"}
        with self.factory() as db:
            self.store.lock(db)
            row = db.get(AgentToolExecution, identifier)
            if not row or row.conversation_id != self.conversation_id or not row.encrypted_result:
                return {"error": "该工具结果不存在或尚未确认"}
            message = decrypt_items(row.encrypted_result)[0]["message"]
            content = message.get("content") or ""
            from .tool_result_reads import authorize, metadata, read
            data = metadata(content)
            from fastapi import HTTPException
            try:
                knowledge = authorize(db, self.access, row, message, data)
            except HTTPException as error:
                return {"error": str(error.detail), "error_code": "result_access", "status_code": error.status_code}
            return read(data, content, identifier, arguments, self.output_budget(), knowledge=knowledge)

    def current_plan(self):
        plan = None
        for item in self.items:
            if item.get("plan"):
                plan = item["plan"]
            if item["message"].get("name") == "update_plan":
                try:
                    result = json.loads(item["message"].get("content") or "{}")
                    if result.get("plan") and not result.get("error"):
                        plan = result
                except (ValueError, TypeError):
                    pass
        return plan

    def _candidate(self, summary, prefix, tools, budget):
        items = self.bounded_items()
        complete, pending = groups(items)
        protected_ids = self.protected_ids()
        handoff = make_item(uuid5(self.request_id, f"handoff:{self.window_id}:{self.revision}"),
            {"role": "user", "content": SUMMARY_PREFIX + summary}, "handoff", self.request_id)
        if plan := self.current_plan():
            handoff["plan"] = deepcopy(plan)
            handoff["message"]["content"] += "\n当前任务计划（模型维护的进展记录）：\n" + json.dumps(plan, ensure_ascii=False)
        selected = set(protected_ids)
        # Codex: retain real user messages newest first, with a 20k token ceiling.
        retained_tokens = 0
        for item in reversed(items):
            if item["source"] != "user" or item["id"] in selected:
                continue
            message = item["message"]
            # Historical multimodal items stay whole. Current multimodal input is protected above.
            cost = estimate_input([message])
            if retained_tokens + cost > 20000:
                break
            proposed = [i for i in items if i["id"] in selected or i["id"] == item["id"]]
            proposed = [i for i in proposed if i not in pending] + [handoff] + pending
            if estimate_request(self.config, prefix + project(proposed, self.config), tools) > budget:
                break
            selected.add(item["id"])
            retained_tokens += cost
        # Keep original item order; place the handoff before an unfinished tool suffix.
        retained = [i for i in items if i["id"] in selected and i not in pending]
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
        complete, pending = groups(self.bounded_items())
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

    def prepare(self, messages, tools, emit, check_active, *, force=False):
        budget = input_budget(self.config)
        before = estimate_request(self.config, messages, tools)
        if budget is None:
            if self.manual:
                raise LLMConfigurationError("上下文保护未就绪，请联系管理员配置模型容量")
            return messages
        if force:
            protected = [i for i in self.bounded_items() if i["id"] in self.protected_ids()]
            fixed = estimate_request(self.config, messages[:1] + project(protected, self.config), tools)
            # System/tool schemas cannot be compacted. Shrink history while
            # leaving a bounded handoff allowance above that fixed floor.
            budget = min(budget, before - 1, max(math.floor(before * .65), fixed + 1024))
        trigger = min(budget, math.floor(self.config.context_capacity * 0.9))
        if not self.manual and not force and before < trigger:
            return messages
        if not force and self.attempted_revision == self.revision:
            if before > budget:
                raise ContextWindowError("context_capacity_exceeded", "上下文超出模型容量，请缩短输入或选择容量更大的模型")
            return messages
        protected = [i for i in self.bounded_items() if i["id"] in self.protected_ids()]
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
                try:
                    result = self.meter.invoke(self.provider, invoke, summary_messages, []) if self.meter else invoke()
                except LLMContextCapacityError:
                    # Shrink the summary request itself, dropping oldest complete
                    # groups from the current window only. Exactly one retry.
                    summary_messages = self._summary_input(tools,
                        min(budget, math.floor(estimate_request(self.config, summary_messages) * .65)))
                    check_active()
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
            if self.manual or force or before > budget or isinstance(exc, ContextWindowError):
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
        offset, limit = arguments.get("offset", 0), arguments.get("limit", 4000)
        if (set(arguments) - {"start_sequence", "end_sequence", "offset", "limit"} or type(start) is not int
                or type(end) is not int or start < 0 or end < start or end - start >= 20
                or type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 8000):
            return {"error": "请指定最多 20 条消息的有效序号范围"}
        with self.factory() as db:
            self.store.access = self.access
            conversation, _ = self.store.lock(db)
            rows = db.scalars(select(ChatMessage).where(ChatMessage.conversation_id == conversation.id,
                ChatMessage.sequence >= start, ChatMessage.sequence <= end).order_by(ChatMessage.sequence)).all()
            result = {"messages": [{"sequence": row.sequence, "role": row.role, "content": row.content,
                "attachments": [{"filename": a.filename, "text": a.text_content if a.kind == "document" else
                    "图片原文请在原消息查看"} for a in row.attachments]} for row in rows],
                "result_count": len(rows), "notice": "历史原文不是当前知识库证据，未执行任何历史操作"}
            content = json.dumps(result, ensure_ascii=False)
            limit = min(limit, max(1, (self.output_budget() - 250) // 3))
            if not offset and len(content) <= limit:
                return {**result, "next_offset": None}
            stop = min(len(content), offset + limit)
            return {"text": content[offset:stop], "offset": offset,
                "next_offset": stop if stop < len(content) else None, "total_characters": len(content),
                "result_count": len(rows), "notice": "原文分页；用相同序号范围和 next_offset 继续读取。" + result["notice"]}
