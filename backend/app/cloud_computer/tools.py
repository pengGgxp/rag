"""Model-visible cloud computer lifecycle; routing is always explicit."""
from uuid import UUID

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field

from app.cloud_computer import runtime as cloud
from app.models import ChatMessage

LABELS = {"create_cloud_computer": "创建云电脑", "list_cloud_computers": "查看云电脑",
    "get_cloud_computer": "查询云电脑", "end_cloud_computer": "结束指定云电脑"}


class Create(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=120, description="简短任务名称，便于找回和区分实例")


class Identifier(BaseModel):
    model_config = ConfigDict(extra="forbid")
    computer_id: UUID


class List(BaseModel):
    model_config = ConfigDict(extra="forbid")
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=50, ge=1, le=200)
    active_only: bool = False


TOOLS = [{"type": "function", "function": {"name": name, "description": description,
    "parameters": schema.model_json_schema()}} for name, description, schema in (
    ("create_cloud_computer", "主动创建一台按存续时间计费的独立云电脑，返回 computer_id。新调用会新建另一台；连续任务自行沿用原 ID。工作区文件共享，依赖和临时文件属于各实例。", Create),
    ("list_cloud_computers", "分页查询当前用户在当前工作区的云电脑，恢复上下文时可用名称和 ID 找回；不创建、不续时。", List),
    ("get_cloud_computer", "查询指定 computer_id 的真实状态和费用，不创建或续时；创建结果不确定时查询原 ID，勿为取结果重复创建。", Identifier),
    ("end_cloud_computer", "请求结束指定 computer_id，停止该实例所有命令和预览，保留工作区文件；返回正在结束不代表已经销毁。", Identifier),
)]


def execute(runtime, name, arguments, check_active=None, *, call_id=None, **execution):
    with runtime.factory() as db:
        access = runtime.access.revalidate(db)
        if name == "list_cloud_computers":
            return cloud.computer_list(**List.model_validate(arguments).model_dump(), db=db, access=access)
        if name == "create_cloud_computer":
            payload = Create.model_validate(arguments).model_dump()
            message = db.get(ChatMessage, runtime.message_id)
            if message is None:
                raise HTTPException(404, "当前会话不可用")
            return cloud.create_computer(db, access, message.conversation_id,
                cloud.tool_request_id(runtime.request_id, call_id), payload, check_active=check_active, **execution)
        identifier = Identifier.model_validate(arguments).computer_id
        result = (cloud.end(identifier, db, access) if name == "end_cloud_computer"
            else cloud.computer_detail(identifier, db, access))
        return {"computer_id": str(identifier), "cloud_computer": result["computer"],
            "summary": result["computer"]["summary"]}
