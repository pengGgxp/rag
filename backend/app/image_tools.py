"""Server-bound image tools; model arguments never select an owner or supplier."""
import hashlib
import json
from uuid import UUID

from fastapi import HTTPException
from pydantic import ValidationError

from .image_tasks import ImageInput, create, owned, read, selected_model

LABELS = {"generate_image": "生成图片", "edit_image": "编辑图片", "get_image_task": "查看图片进度"}


def tools(db, access):
    result = []
    if access.role not in ("admin", "editor"):
        return result
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
            "description": "Generate/edit images only when explicitly requested by the user. This charges credits. Return task progress, never claim completion before completed. Use defaults when unspecified; valid specifications: " + json.dumps([
                {"size": s.size, "quality": s.quality, "credits_per_image": str(s.credits_per_image)} for s in specs]),
            "parameters": {"type": "object", "properties": properties, "required": ["prompt", *(["references"] if operation == "edit" else [])], "additionalProperties": False}}})
    if result:
        result.append({"type": "function", "function": {"name": "get_image_task", "description": "Read an existing image task only when needed; the UI tracks progress without model polling.",
            "parameters": {"type": "object", "properties": {"task_id": {"type": "string"}}, "required": ["task_id"], "additionalProperties": False}}})
    return result


def execute(runtime, name, arguments):
    with runtime.factory() as db:
        access = runtime.access.revalidate(db)
        try:
            if name == "get_image_task":
                if set(arguments) != {"task_id"}:
                    raise ValueError()
                return read(owned(db, access, UUID(arguments["task_id"])))
            payload = ImageInput.model_validate({**arguments, "operation": "edit" if name == "edit_image" else "generate"})
            if "operation" in arguments or not runtime.request_id:
                raise ValueError()
            key = hashlib.sha256((str(runtime.request_id) + ":" + name + ":" + json.dumps(arguments, sort_keys=True)).encode()).hexdigest()
            return create(db, access, payload, key)
        except (ValidationError, ValueError, TypeError):
            return {"error": "图片参数不完整，请检查提示词、规格和参考资源"}
        except HTTPException as exc:
            return {"error": str(exc.detail)}
