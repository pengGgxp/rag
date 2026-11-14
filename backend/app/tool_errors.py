"""Preserve actionable tool errors without publishing common credential forms."""
import re


def public_error(detail, fallback="本次工具操作未完成"):
    if isinstance(detail, dict):
        detail = detail.get("message") or detail.get("error")
    value = str(detail or fallback)
    value = re.sub(r"(?i)(https?://)[^/\s@]+@", r"\1[已隐藏]@", value)
    value = re.sub(r"(?i)\bBearer\s+[^\s,;]+", "Bearer [已隐藏]", value)
    value = re.sub(r"(?i)(\b(?:api[_-]?key|access[_-]?key|secret|password|token|authorization)\b[\s\"']*[:=][\s\"']*)[^\s,;\"'&]+",
        r"\1[已隐藏]", value)
    return value[:1000]
