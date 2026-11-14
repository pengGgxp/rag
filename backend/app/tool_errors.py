"""Preserve actionable tool errors without publishing common credential forms."""
import re


ARGUMENT_ERROR_CODES = frozenset((
    "tool_arguments", "knowledge_arguments", "preview_arguments", "document_arguments",
    "file_arguments", "image_arguments", "plan_arguments",
))
BLOCKED_ERROR_CODES = frozenset((
    "tool_access", "knowledge_access", "preview_unavailable", "cloud_computer_unavailable",
    "file_capacity", "file_tool_unavailable", "image_model_unsupported", "tool_unavailable",
))


def tool_outcome(result):
    """Classify presentation separately from the tool observation sent to the model."""
    if result.get("uncertain"):
        return "uncertain"
    if not (result.get("error") or result.get("isError")):
        return "success"
    code, status = result.get("error_code"), result.get("status_code")
    if status in (401, 402, 403):
        return "blocked"
    if code in ARGUMENT_ERROR_CODES or status == 422:
        return "feedback"
    if code in BLOCKED_ERROR_CODES:
        return "blocked"
    return "failed"


def public_error(detail, fallback="本次工具操作未完成"):
    if isinstance(detail, dict):
        detail = detail.get("message") or detail.get("error")
    value = str(detail or fallback)
    value = re.sub(r"(?i)(https?://)[^/\s@]+@", r"\1[已隐藏]@", value)
    value = re.sub(r"(?i)\bBearer\s+[^\s,;]+", "Bearer [已隐藏]", value)
    value = re.sub(r"(?i)(\b(?:api[_-]?key|access[_-]?key|secret|password|token|authorization)\b[\s\"']*[:=][\s\"']*)[^\s,;\"'&]+",
        r"\1[已隐藏]", value)
    return value[:1000]
