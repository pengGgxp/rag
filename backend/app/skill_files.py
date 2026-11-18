"""Bounded, in-memory Skill directories. No uploaded path is used on disk."""

import io
import lzma
import mimetypes
import stat
import tarfile
import unicodedata
import zipfile
import zlib
from hashlib import sha256

from fastapi import HTTPException

MAX_SIZE_MIB = 200
MAX_FILE_BYTES = MAX_BUNDLE_BYTES = MAX_UPLOAD_BYTES = MAX_SIZE_MIB * 1024 * 1024
MAX_ENCODED_FILE_CHARS = 4 * ((MAX_FILE_BYTES + 2) // 3)

SCRIPT_SUFFIXES = (".py", ".js", ".mjs", ".cjs", ".sh", ".bash")
WINDOWS_RESOURCE_SUFFIXES = (".bat", ".cmd", ".com", ".exe", ".msi", ".ps1")
IMAGE_MEDIA_TYPES = {"image/jpeg", "image/png", "image/webp"}
ARCHIVE_SUFFIXES = (".tar", ".tar.gz", ".tgz", ".zip")


def safe_path(path: str) -> str:
    if (
        not path or len(path) > 512 or path != unicodedata.normalize("NFC", path)
        or any(ord(c) < 32 or ord(c) == 127 for c in path)
        or any(c in path for c in "\\:")
        or any(part in ("", ".", "..") for part in path.split("/"))
    ):
        raise HTTPException(422, "文件路径必须是 Skill 内的相对路径，不能包含 ..、反斜杠或空目录")
    return path


def text_content(content: bytes) -> str | None:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        return None
    return None if any(ord(c) < 32 and c not in "\t\r\n" for c in text) else text


def file_kind(path: str, content: bytes) -> str:
    name = path.lower()
    media_type = mimetypes.guess_type(path)[0] or ""
    if name.endswith(SCRIPT_SUFFIXES) and text_content(content) is not None:
        return "script"
    if media_type in IMAGE_MEDIA_TYPES:
        return "image"
    if name.endswith(ARCHIVE_SUFFIXES):
        return "archive"
    if text_content(content) is not None:
        return "text"
    return "binary"


def manifest(files: dict[str, bytes]) -> list[dict]:
    return [
        {
            "path": path, "size": len(content),
            "media_type": mimetypes.guess_type(path)[0] or "application/octet-stream",
            "text": text_content(content) is not None, "sha256": sha256(content).hexdigest(),
            "kind": file_kind(path, content),
        }
        for path, content in sorted(files.items())
    ]


def validate_files(files: dict[str, bytes]) -> dict[str, bytes]:
    if not files:
        raise HTTPException(422, "Skill 至少需要包含一个文件")
    seen = set()
    total = 0
    for path, content in files.items():
        safe_path(path)
        key = path.casefold()
        if key in seen:
            raise HTTPException(422, "Skill 存在大小写冲突的重复路径")
        seen.add(key)
        if len(content) > MAX_FILE_BYTES:
            raise HTTPException(413, f"单个 Skill 文件不能超过 {MAX_SIZE_MIB} MiB")
        total += len(content)
    if any("/".join(key.split("/")[:i]) in seen for key in seen for i in range(1, len(key.split("/")))):
        raise HTTPException(422, "Skill 路径存在文件与目录冲突")
    if total > MAX_BUNDLE_BYTES:
        raise HTTPException(413, f"Skill 总大小不能超过 {MAX_SIZE_MIB} MiB")
    if "SKILL.md" not in files or text_content(files["SKILL.md"]) is None:
        raise HTTPException(422, "Skill 根目录必须包含 UTF-8 编码的 SKILL.md")
    return files


def _reject_raw_zip_paths(content: bytes) -> None:
    """Inspect central-directory names before zipfile normalizes Windows separators."""
    cursor = 0
    marker = b"PK\x01\x02"
    while True:
        start = content.find(marker, cursor)
        if start < 0:
            return
        if start + 46 > len(content):
            raise HTTPException(422, "ZIP 无效、损坏或压缩方式不支持")
        name_length = int.from_bytes(content[start + 28:start + 30], "little")
        extra_length = int.from_bytes(content[start + 30:start + 32], "little")
        comment_length = int.from_bytes(content[start + 32:start + 34], "little")
        end = start + 46 + name_length + extra_length + comment_length
        if end > len(content):
            raise HTTPException(422, "ZIP 无效、损坏或压缩方式不支持")
        raw_name = content[start + 46:start + 46 + name_length]
        if b"\\" in raw_name or b"\x00" in raw_name:
            raise HTTPException(422, "文件路径必须是 Skill 内的相对路径，不能包含 ..、反斜杠或空目录")
        cursor = end


def imported_files(entries: list[tuple[str, bytes]]) -> tuple[dict[str, bytes], str | None]:
    entries = [(safe_path(path), content) for path, content in entries]
    paths = [path for path, _ in entries]
    if len(paths) != len(set(path.casefold() for path in paths)):
        raise HTTPException(422, "Skill 存在重复文件路径")
    skill_roots = {
        path.removesuffix("SKILL.md").rstrip("/") for path in paths
        if path == "SKILL.md" or path.endswith("/SKILL.md")
    }
    if not skill_roots:
        raise HTTPException(422, "请选择包含 SKILL.md 的 Skill 文件夹或压缩包，或选择其上级目录")
    if len(skill_roots) != 1:
        raise HTTPException(422, "检测到多个 Skill 根目录；请选择包含所需 SKILL.md 的文件夹或压缩包")
    root = skill_roots.pop()
    prefix = f"{root}/" if root else ""
    entries = [(path.removeprefix(prefix), content) for path, content in entries if path.startswith(prefix)]
    folder = root.rsplit("/", 1)[-1] if root else None
    return validate_files(dict(entries)), folder


def unzip(content: bytes) -> tuple[dict[str, bytes], str | None]:
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, f"ZIP 不能超过 {MAX_SIZE_MIB} MiB")
    try:
        _reject_raw_zip_paths(content)
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            infos = archive.infolist()
            entries, total = [], 0
            for info in infos:
                safe_path(info.orig_filename.rstrip("/") if info.is_dir() else info.orig_filename)
                safe_path(info.filename.rstrip("/") if info.is_dir() else info.filename)
                mode = info.external_attr >> 16
                kind = stat.S_IFMT(mode)
                if kind not in (0, stat.S_IFREG, stat.S_IFDIR) or info.flag_bits & 1:
                    raise HTTPException(422, "ZIP 不允许链接、特殊文件或加密条目")
                if info.is_dir():
                    continue
                total += info.file_size
                if info.file_size > MAX_FILE_BYTES or total > MAX_BUNDLE_BYTES:
                    raise HTTPException(413, "ZIP 超出 Skill 文件大小限制")
                with archive.open(info) as source:
                    data = source.read(MAX_FILE_BYTES + 1)
                if len(data) != info.file_size:
                    raise HTTPException(422, "ZIP 文件大小不一致")
                entries.append((info.filename, data))
            return imported_files(entries)
    except (zipfile.BadZipFile, zlib.error, lzma.LZMAError, RuntimeError, NotImplementedError, EOFError, ValueError) as exc:
        raise HTTPException(422, "ZIP 无效、损坏或压缩方式不支持") from exc


def archive_entries(path: str, content: bytes) -> list[dict]:
    """Inspect archive members in memory without extracting or recursing."""
    name = path.lower()
    try:
        if name.endswith(".zip"):
            _reject_raw_zip_paths(content)
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                infos = archive.infolist()
                entries, total = [], 0
                for info in infos:
                    member = info.filename.rstrip("/") if info.is_dir() else info.filename
                    safe_path(info.orig_filename.rstrip("/") if info.is_dir() else info.orig_filename)
                    safe_path(member)
                    mode = info.external_attr >> 16
                    if stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR) or info.flag_bits & 1:
                        raise HTTPException(422, "压缩包不允许链接、特殊文件或加密条目")
                    if info.is_dir():
                        continue
                    total += info.file_size
                    if info.file_size > MAX_FILE_BYTES or total > MAX_BUNDLE_BYTES:
                        raise HTTPException(413, "压缩包超出 Skill 文件大小限制")
                    with archive.open(info) as source:
                        data = source.read(MAX_FILE_BYTES + 1)
                    if len(data) != info.file_size:
                        raise HTTPException(422, "压缩包文件大小不一致")
                    entries.append({"path": member, "size": info.file_size})
                return entries
        if name.endswith((".tar", ".tar.gz", ".tgz")):
            entries, total = [], 0
            with tarfile.open(fileobj=io.BytesIO(content), mode="r:*") as archive:
                for info in archive:
                    safe_path(info.name.rstrip("/") if info.isdir() else info.name)
                    if not (info.isfile() or info.isdir()):
                        raise HTTPException(422, "TAR 不允许链接或特殊文件")
                    if info.isdir():
                        continue
                    total += info.size
                    if info.size > MAX_FILE_BYTES or total > MAX_BUNDLE_BYTES:
                        raise HTTPException(413, "TAR 超出 Skill 文件大小限制")
                    source = archive.extractfile(info)
                    if source is None or len(source.read(MAX_FILE_BYTES + 1)) != info.size:
                        raise HTTPException(422, "压缩包文件大小不一致")
                    entries.append({"path": info.name, "size": info.size})
            return entries
    except (zipfile.BadZipFile, tarfile.TarError, zlib.error, lzma.LZMAError, RuntimeError,
            NotImplementedError, EOFError, ValueError) as exc:
        raise HTTPException(422, "示例压缩包无效、损坏或压缩方式不支持") from exc
    raise HTTPException(422, "该文件不是可检查的示例压缩包")


def bundle_files(row) -> dict[str, bytes]:
    return {"SKILL.md": row.markdown.encode("utf-8"), **{item.path: item.content for item in row.files}}


def export_zip(name: str, files: dict[str, bytes]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path, content in sorted(files.items()):
            archive.writestr(f"{name}/{path}", content)
    return output.getvalue()
