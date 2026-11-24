"""Review package resources without extracting or executing uploaded files."""
import base64
import binascii

from fastapi import HTTPException

from app.skills import files as skill_files
from app.skills.files import WINDOWS_RESOURCE_SUFFIXES, archive_entries, manifest, safe_path, text_content, validate_files

TEXT_SUFFIXES = {"md", "txt", "csv", "json", "yaml", "yml", "xml", "html", "css", "svg"}


def decode_files(items, markdown):
    if sum(len(item.content) for item in items) > (skill_files.MAX_BUNDLE_BYTES * 4 // 3 + len(items) * 4):
        raise HTTPException(413, f"Skill 文件总大小不能超过 {skill_files.MAX_SIZE_MIB} MiB")
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
    return {"manifest": entries, "issues": list(dict.fromkeys(issues)), "can_enable": not issues}
