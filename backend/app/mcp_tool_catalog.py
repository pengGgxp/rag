"""Authorized metadata discovery and budgeted, per-runtime MCP definitions."""
import json
from collections import OrderedDict

from fastapi import HTTPException
from sqlalchemy import select

from . import mcp_client
from .models import MCPServer
from .personal_skills import owned


def definition(name, description, properties, required=()):
    return {"type": "function", "function": {"name": name, "description": description,
        "parameters": {"type": "object", "properties": properties,
            "required": list(required), "additionalProperties": False}}}


TOOLS = [
    definition("search_mcp_tools", "Search metadata of currently authorized MCP tools. Search results do not load definitions. Use load_mcp_tool for a selected alias before calling it in a later model step.",
        {"query": {"type": "string"}, "offset": {"type": "integer", "minimum": 0},
            "limit": {"type": "integer", "minimum": 1, "maximum": 50}}),
    definition("load_mcp_tool", "Load an authorized MCP tool definition for the next model step. Older definitions may be evicted to fit model input. This does not execute the remote tool.",
        {"alias": {"type": "string"}}, ["alias"]),
]
NAMES = {item["function"]["name"] for item in TOOLS}


class Catalog:
    def __init__(self, runtime):
        self.runtime = runtime
        self.loaded = OrderedDict()
        self.offered = set()
        self.restored = False

    def check(self, alias):
        item = self.runtime.bindings.get(alias)
        if item is None:
            raise mcp_client.MCPError("工具不存在或未授权，请重新搜索工具")
        with self.runtime.factory() as db:
            access = self.runtime.access.revalidate(db)
            row = owned(db, MCPServer, item["id"], access)
            if (not row.enabled or row.authorization_version != item["authorization"]
                    or item["tool"]["name"] not in row.allowed_tools or item["tool"] not in row.tools
                    or (row.url, row.transport, row.encrypted_headers) != (item["url"], item["transport"], item["credentials"])):
                raise mcp_client.MCPError("连接或授权已变化，本次操作未执行")
        return item

    @property
    def budget(self):
        from .conversation_context import input_budget
        config = self.runtime.provider_config
        budget = input_budget(config) if config else None
        return (24000 if budget is None else budget) // 4

    def schema(self, alias, item):
        return {"type": "function", "function": {"name": alias,
            "description": f"{item['name']} / {item['tool']['name']}: {item['tool'].get('description', '')}",
            "parameters": item["tool"]["inputSchema"]}}

    def cost(self, schemas):
        from .conversation_context import estimate_input, estimate_request
        config = self.runtime.provider_config
        if config:
            return max(0, estimate_request(config, [], schemas) - estimate_request(config, []))
        return estimate_input([], schemas)

    def refresh(self):
        self.runtime.tools[:] = [tool for tool in self.runtime.tools
            if not tool["function"]["name"].startswith("mcp_")] + list(self.loaded.values())

    def load(self, alias):
        item = self.check(alias)
        schema = self.schema(alias, item)
        mcp_client.validate_tool(item["tool"])
        if self.cost([schema]) > self.budget:
            return {"error": "单个工具定义超出当前模型的工具输入预算，请选择容量更大的模型",
                "error_code": "mcp_definition_capacity", "alias": alias, "budget_tokens": self.budget}
        self.loaded.pop(alias, None)
        evicted = []
        while self.loaded and self.cost([*self.loaded.values(), schema]) > self.budget:
            name, _ = self.loaded.popitem(last=False)
            evicted.append(name)
        self.loaded[alias] = schema
        self.refresh()
        return {"loaded_alias": alias, "definition_digest": mcp_client.tool_digest(item["tool"]),
            "evicted_aliases": evicted, "available": "next_model_step",
            "summary": f"已加载「{item['name']} · {item['tool']['name']}」"}

    def prepare(self, messages):
        if not self.restored:
            for message in messages:
                if message.get("role") != "tool":
                    continue
                try:
                    data = json.loads(message.get("content") or "{}")
                    if message.get("name") == "load_mcp_tool" and data.get("loaded_alias"):
                        alias = data["loaded_alias"]
                        item = self.runtime.bindings.get(alias)
                        if item and data.get("definition_digest") == mcp_client.tool_digest(item["tool"]):
                            self.load(alias)
                        for removed in data.get("evicted_aliases", []):
                            self.loaded.pop(removed, None)
                    elif message.get("name") in self.loaded:
                        self.loaded.move_to_end(message["name"])
                except (ValueError, TypeError, HTTPException, mcp_client.MCPError):
                    continue
            self.restored = True
        for alias in list(self.loaded):
            try:
                self.check(alias)
            except (HTTPException, mcp_client.MCPError):
                self.loaded.pop(alias, None)
        while self.loaded and self.cost(list(self.loaded.values())) > self.budget:
            self.loaded.popitem(last=False)
        self.refresh()
        self.offered = set(self.loaded)
        return self.runtime.tools

    def execute(self, name, arguments):
        from jsonschema import Draft202012Validator, ValidationError
        schema = next(tool["function"]["parameters"] for tool in TOOLS if tool["function"]["name"] == name)
        try:
            Draft202012Validator(schema).validate(arguments)
            if name == "load_mcp_tool":
                return self.load(arguments["alias"])
            query = arguments.get("query", "").strip().casefold()
            offset, limit = arguments.get("offset", 0), arguments.get("limit", 20)
            matches = []
            with self.runtime.factory() as db:
                access = self.runtime.access.revalidate(db)
                servers = {row.id: row for row in db.scalars(select(MCPServer).where(
                    MCPServer.owner_id == access.user_id, MCPServer.account_id == access.user_id,
                    MCPServer.enabled.is_(True)))}
                current = {identifier: {tool["name"]: tool for tool in row.tools}
                    for identifier, row in servers.items()}
            for alias, item in self.runtime.bindings.items():
                row = servers.get(item["id"])
                if (not row or row.authorization_version != item["authorization"]
                        or item["tool"]["name"] not in row.allowed_tools
                        or current[row.id].get(item["tool"]["name"]) != item["tool"]
                        or (row.url, row.transport, row.encrypted_headers) != (item["url"], item["transport"], item["credentials"])):
                    continue
                metadata = {"alias": alias, "server": item["name"], "name": item["tool"]["name"],
                    "description": item["tool"].get("description", "")[:600]}
                if not query or query in " ".join(metadata.values()).casefold():
                    matches.append(metadata)
            end = min(len(matches), offset + limit)
            return {"tools": matches[offset:end], "offset": offset, "total": len(matches),
                "next_offset": end if end < len(matches) else None}
        except ValidationError:
            return {"error": "请检查工具别名、搜索词和分页参数", "error_code": "mcp_discovery_arguments"}
        except (HTTPException, mcp_client.MCPError) as error:
            return {"error": str(error.detail) if isinstance(error, HTTPException) else str(error),
                "error_code": "mcp_discovery_access"}
