"""Review package resources without extracting or executing uploaded files."""
import base64
import binascii
import posixpath
import re
from urllib.parse import unquote, urlsplit

from fastapi import HTTPException

from .skill_files import MAX_BUNDLE_BYTES, WINDOWS_RESOURCE_SUFFIXES, archive_entries, manifest, safe_path, text_content, validate_files

TEXT_SUFFIXES = {"md", "txt", "csv", "json", "yaml", "yml", "xml", "html", "css", "svg"}
LOCAL_LINK = re.compile(r"!?\[[^\[\]\r\n]*\]\(<?([^\s)<>]+)>?(?:[ \t]+[^()\r\n]*)?\)")


def decode_files(items, markdown):
    if sum(len(item.content) for item in items) > (MAX_BUNDLE_BYTES * 4 // 3 + len(items) * 4):
        raise HTTPException(413, "Skill 文件总大小不能超过 20 MiB")
    files = {"SKILL.md": markdown.encode("utf-8")}
    seen = {"skill.md"}
    for item in items:
        path = safe_path(item.path)
        if path.casefold() in seen:
            raise HTTPException(422, "Skill 包存在重复路径；SKILL.md 请在指令正文中编辑")
        seen.add(path.casefold())
        try:
            files[path] = base64.b64decode(item.content, validate=True)
        except (ValueError, binascii.Error):
            raise HTTPException(422, "文件内容无效，请重新导入") from None
    return validate_files(files)


def encode_files(files):
    return [{"path": path, "content": base64.b64encode(content).decode("ascii")}
            for path, content in sorted(files.items()) if path != "SKILL.md"]


def review(files):
    entries, issues = manifest(files), []
    for entry in entries:
        path = entry["path"]
        entry["readable"] = (entry["kind"] == "text" and path.rsplit(".", 1)[-1].lower() in TEXT_SUFFIXES
                             and "scripts" not in path.lower().split("/"))
        entry["resource"] = (entry["kind"] in {"script", "image", "archive", "binary"}
                             or path.lower().endswith(WINDOWS_RESOURCE_SUFFIXES))
        if entry["kind"] == "archive":
            try:
                entry["archive_entries"] = archive_entries(path, files[path])
            except HTTPException as exc:
                entry["archive_error"] = str(exc.detail)
                entry["blocking"] = True
                issues.append(f"{path}：{exc.detail}")
        if path.lower().endswith(".md") and entry["readable"]:
            for match in LOCAL_LINK.finditer(text_content(files[path])):
                try:
                    target = urlsplit(match[1])
                except ValueError:
                    issues.append(f"{path}：存在无法识别的资料链接，请修改后再启用")
                    continue
                if target.scheme or target.netloc or not target.path:
                    continue
                resolved = posixpath.normpath(posixpath.join(posixpath.dirname(path), unquote(target.path)))
                if resolved not in files:
                    issues.append(f"{path}：引用的文件 {target.path} 不在包内，请补齐或修改指令")
    return {"manifest": entries, "issues": list(dict.fromkeys(issues)), "can_enable": not issues}
