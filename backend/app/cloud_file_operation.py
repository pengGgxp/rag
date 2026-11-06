"""Runs inside the cloud computer; contains no storage SDK or credentials."""
import hashlib
import json
import os
from pathlib import Path


def apply_patch(original, patch):
    # Keep the patch grammar identical to the public file tool.
    import re
    lines, changes = original.splitlines(keepends=True), patch.splitlines(keepends=True)
    out, cursor, index, hunks = [], 0, 0, 0
    while index < len(changes):
        line = changes[index]
        if not line.startswith("@@"):
            index += 1
            continue
        match = re.match(r"@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@", line)
        if not match:
            raise ValueError("补丁格式无效")
        start = max(0, int(match.group(1)) - 1)
        if start < cursor or start > len(lines):
            raise RuntimeError("文件已变化，请重新读取")
        out.extend(lines[cursor:start]); cursor = start; index += 1; hunks += 1
        while index < len(changes) and not changes[index].startswith("@@"):
            item = changes[index]; index += 1
            if item.startswith("\\ No newline"):
                continue
            kind, value = item[:1], item[1:]
            if kind in (" ", "-"):
                if cursor >= len(lines) or lines[cursor] != value:
                    raise RuntimeError("文件已变化，请重新读取")
                if kind == " ": out.append(value)
                cursor += 1
            elif kind == "+":
                out.append(value)
            else:
                raise ValueError("补丁格式无效")
    if not hunks:
        raise ValueError("补丁缺少修改位置")
    return "".join(out + lines[cursor:])


def safe_path(root, relative):
    path = Path(relative)
    if path.is_absolute() or not relative or any(part in ("", ".", "..") for part in relative.split("/")) or "\\" in relative:
        raise ValueError("文件路径无效")
    target = root / path
    if any(item.is_symlink() for item in (target, *target.parents)):
        raise ValueError("文件路径不能为符号链接")
    return target


def write_mounted(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as handle:
        handle.write(data)


def perform(operation, root=Path("/home/user/workspace")):
    name = operation["name"]
    if name == "list_files":
        items = []
        for directory, dirs, files in os.walk(root, followlinks=False):
            dirs[:] = sorted(name for name in dirs if name not in {"node_modules", ".venv", "venv", "__pycache__", ".cache", ".git"} and not Path(directory, name).is_symlink())
            for filename in sorted(files):
                path = Path(directory, filename)
                if path.is_symlink() or not path.is_file(): continue
                relative = path.relative_to(root).as_posix()
                if operation.get("query", "").lower() in relative.lower():
                    items.append({"path": relative, "size_bytes": path.stat().st_size})
        items.sort(key=lambda item: item["path"])
        offset, limit = operation.get("offset", 0), operation.get("limit", 100)
        return {"items": items[offset:offset+limit], "offset": offset, "limit": limit, "has_more": len(items) > offset+limit}
    path = safe_path(root, operation["path"])
    if name == "read_file":
        if not path.is_file(): raise FileNotFoundError("文件不存在或已删除")
        if operation.get("binary"): return {"file_path": str(path), "size_bytes": path.stat().st_size}
        offset = min(operation.get("offset", 0), path.stat().st_size)
        with path.open("rb") as handle:
            handle.seek(offset); data = handle.read(operation.get("limit", 12000))
        return {"content": data.decode("utf-8", errors="replace"), "offset": offset,
                "next_offset": offset + len(data) if offset + len(data) < path.stat().st_size else None}
    if name == "delete_file":
        path.unlink(); return {"path": operation["path"], "deleted": True}
    content = operation.get("content", "")
    if name == "apply_patch": content = apply_patch(path.read_text(encoding="utf-8"), operation["patch"])
    write_mounted(path, content.encode("utf-8"))
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1048576), b""): digest.update(block)
    return {"path": operation["path"], "size_bytes": path.stat().st_size, "sha256": digest.hexdigest()}


if __name__ == "__main__":
    import sys
    job = Path(sys.argv[1])
    try:
        result = {"value": perform(json.loads((job / "operation.json").read_text()))}
    except (ValueError, UnicodeError) as error:
        result = {"error": str(error), "status_code": 422}
    except FileNotFoundError as error:
        result = {"error": str(error), "status_code": 404}
    except RuntimeError as error:
        result = {"error": str(error), "status_code": 409}
    (job / "file-result.json").write_text(json.dumps(result, ensure_ascii=True))
