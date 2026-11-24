"""绑定服务端身份的图片工具，不允许模型参数选择用户或供应商。"""
import json
from uuid import UUID

from fastapi import HTTPException
from pydantic import ValidationError

from .schemas import ImageInput
from .tasks import create, owned, run, selected_model, tool_read

LABELS = {"generate_image": "生成图片", "edit_image": "编辑图片"}


def tools(db, access):
    result = []
    for name, operation in (("generate_image", "generate"), ("edit_image", "edit")):
        try:
            _, _, config = selected_model(db, operation)
        except (HTTPException, ValidationError):
            continue
        specs = [s for s in config.specs if s.operation == operation]
        properties = {"prompt": {"type": "string", "maxLength": 12000},
            "count": {"type": "integer", "minimum": 1, "maximum": config.max_count},
            "size": {"type": "string", "enum": sorted({s.size for s in specs})},
            "quality": {"type": "string", "enum": sorted({s.quality for s in specs})}}
        if operation == "edit":
            properties["references"] = {"type": "array", "minItems": 1, "maxItems": config.max_references,
                "items": {"type": "object", "properties": {"id": {"type": "string"}, "version": {"type": "integer", "minimum": 1}},
                          "required": ["id", "version"], "additionalProperties": False}}
        result.append({"type": "function", "function": {"name": name,
            "description": "Generate/edit images only when explicitly requested by the user. This charges credits. The tool waits and returns the actual final result and saved resources; use those resources to continue the user's task. Never regenerate an uncertain result. Read the image if visual inspection is needed; saved resources alone do not mean you have seen it. Omit size/quality to use defaults " + json.dumps(dict(zip(("size", "quality"), config.default_for(operation)))) + ". For Gemini size means aspect ratio and quality means resolution. Valid specifications: " + json.dumps([
                {"size": s.size, "quality": s.quality, "credits_per_image": str(s.credits_per_image)} for s in specs]),
            "parameters": {"type": "object", "properties": properties, "required": ["prompt", *(["references"] if operation == "edit" else [])], "additionalProperties": False}}})
    return result


def execute(runtime, name, arguments, *, agent_tool_execution_id=None, check_active=None, on_progress=None):
    with runtime.factory() as db:
        access = runtime.access.revalidate(db)
        try:
            payload = ImageInput.model_validate({**arguments, "operation": "edit" if name == "edit_image" else "generate"})
            if "operation" in arguments or not runtime.request_id or agent_tool_execution_id is None:
                raise ValueError()
            from ..agent.tool_jobs import validate_owner
            validate_owner(db, agent_tool_execution_id, access, request_id=runtime.request_id)
            task = create(db, access, payload, str(agent_tool_execution_id),
                agent_tool_execution_id=agent_tool_execution_id)
            def progress(value):
                if on_progress:
                    on_progress({"image_task": value, "summary": value["summary"]})
            from .tasks import notify
            notify(owned(db, access, UUID(task["task_id"])), progress)
            db.commit()
            run(task["task_id"], runtime.factory, agent_tool_execution_id=agent_tool_execution_id,
                check_active=check_active, on_progress=progress)
            db.expire_all()
            return tool_read(owned(db, runtime.access.revalidate(db), UUID(task["task_id"])))
        except (ValidationError, ValueError, TypeError):
            return {"error": "图片参数不完整，请检查提示词、规格和参考资源", "error_code": "image_arguments"}
        except HTTPException as exc:
            from ..tool_errors import public_error
            return {"error": public_error(exc.detail), "status_code": exc.status_code}
