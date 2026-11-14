"""Workspace namespace operations. This module has no Skill dependencies."""
import hashlib
import mimetypes
from pathlib import PurePosixPath
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import select

from . import file_operations as ops, shared_resources as shared
from .auth import now
from .models import ChatAttachment, WorkspaceDirectory, WorkspaceFile, WorkspaceFileVersion


def snapshot(db, access, *, refresh=True):
    with ops.workspace_lock(db, access.workspace_id):
        return _snapshot(db, access, refresh=refresh)


def _snapshot(db, access, *, refresh):
    if refresh:
        pending = list(db.scalars(select(ops.WorkspaceFileOperation).where(
            ops.WorkspaceFileOperation.workspace_id == access.workspace_id,
            ops.WorkspaceFileOperation.state.in_(("pending", "recovery_required")))))
        for operation in pending:
            if not ops.restore(db, ops.storage(db), operation):
                raise HTTPException(409, "工作区有未完成的文件恢复，请稍后重试；持续失败请联系管理员")
        shared.refresh_index(db, access)
    files = {row.path: row for row in db.scalars(select(WorkspaceFile).where(
        WorkspaceFile.workspace_id == access.workspace_id, WorkspaceFile.deleted_at.is_(None)))}
    explicit = {row.path: row for row in db.scalars(select(WorkspaceDirectory).where(
        WorkspaceDirectory.workspace_id == access.workspace_id))}
    directories = set(explicit)
    for path in [*files, *explicit]:
        directories.update(str(parent) for parent in PurePosixPath(path).parents if str(parent) != ".")
    return files, directories, explicit


def beneath(path, root):
    return path == root or path.startswith(root + "/")


def validate_destination(path, files, directories):
    from .workspace_files import valid_path
    valid_path(path)
    for parent in PurePosixPath(path).parents:
        if str(parent) in files:
            raise HTTPException(409, "上级路径是文件，请选择其他文件夹")


def check_capacity(db, access, amount):
    from .storage_billing import check_capacity as check
    check(db, access.workspace_id, max(0, amount))


def tree_revision(paths, files, directories):
    return ops.fingerprint({"files": [(path, ops.file_revision(row)) for path, row in sorted(files.items())
        if any(beneath(path, root) for root in paths)],
        "directories": sorted(path for path in directories if any(beneath(path, root) for root in paths))})


def normalize_selection(paths):
    from .workspace_files import valid_path
    unique = sorted(set(valid_path(path) for path in paths), key=lambda path: (path.count("/"), path))
    return [path for path in unique if not any(path.startswith(root + "/") for root in unique if root != path)]


def delete_preview(db, access, paths):
    paths = normalize_selection(paths)
    files, directories, _ = snapshot(db, access)
    if any(path not in files and path not in directories for path in paths):
        raise HTTPException(404, "部分项目已删除，请刷新后重新选择")
    return {"paths": paths, "files": sum(any(beneath(path, root) for root in paths) for path in files),
            "directories": sum(any(beneath(path, root) for root in paths) for path in directories),
            "confirmation": tree_revision(paths, files, directories), "shared": True}


def _mark_deleted(db, access, row):
    row.deleted_at = now()
    for attachment in db.scalars(select(ChatAttachment).where(ChatAttachment.resource_file_id == row.id)):
        attachment.deleted_at = attachment.object_deleted_at = row.deleted_at


def delete(db, access, paths, *, confirmation=None, request_id=None):
    access.editor()
    paths = normalize_selection(paths)
    signature = ops.fingerprint(["delete", paths, confirmation])
    with ops.workspace_lock(db, access.workspace_id):
        previous = ops.replay(db, access, request_id, signature)
        if previous is not None:
            return previous
        preview = delete_preview(db, access, paths)
        if confirmation is not None and confirmation != preview["confirmation"]:
            raise HTTPException(409, {"message": "内容已变化，请重新确认删除范围", "preview": preview})
        files, _, explicit = snapshot(db, access, refresh=False)
        selected_files = [row for path, row in files.items() if any(beneath(path, root) for root in paths)]
        selected_dirs = [row for path, row in explicit.items() if any(beneath(path, root) for root in paths)]
        changes = [{"key": shared.object_key(access.workspace_id, row.path), "delete": True,
                    "expected": ops.file_revision(row)} for row in selected_files]
        store = ops.storage(db)
        changes += [{"key": shared.directory_marker_key(access.workspace_id, row.path), "delete": True,
                     "expected": ops.revision(store.head(shared.directory_marker_key(access.workspace_id, row.path)))} for row in selected_dirs]
        def update():
            for row in selected_files:
                _mark_deleted(db, access, row)
            for row in selected_dirs:
                db.delete(row)
            return {"deleted": paths, "files": preview["files"], "directories": preview["directories"]}
        return ops.execute(db, access, changes, update, request_id=request_id, signature=signature)


def save_files(db, access, files, *, expected=None, request_id=None, source="resource_upload", signature=None):
    """Create a package or save files, with one quota check and one journal."""
    access.editor()
    expected = expected or {}
    from .workspace_files import MAX_BYTES, read_file
    signature = signature or ops.fingerprint(["write", source, expected,
        [(path, hashlib.sha256(data).hexdigest()) for path, (data, _) in sorted(files.items())]])
    with ops.workspace_lock(db, access.workspace_id):
        previous = ops.replay(db, access, request_id, signature)
        if previous is not None:
            return previous
        current, directories, _ = snapshot(db, access)
        changes, increase = [], 0
        for path, (data, content_type) in files.items():
            validate_destination(path, current, directories)
            if len(data) > MAX_BYTES:
                raise HTTPException(413, "文件不能超过 200 MiB")
            if path in directories:
                raise HTTPException(409, "该名称已用于文件夹，请使用其他名称")
            row = current.get(path)
            actual = ops.file_revision(row) if row else None
            if actual != expected.get(path):
                raise HTTPException(409, {"message": "文件已变化或存在同名文件，请重新加载后选择",
                    "conflicts": [{"path": path, "revision": actual, "kind": "file"}]})
            increase += len(data) - (row.size_bytes if row else 0)
            changes.append({"key": shared.object_key(access.workspace_id, path), "data": data,
                "content_type": content_type, "expected": actual})
        check_capacity(db, access, increase)
        def update():
            result = []
            for path, (data, content_type) in files.items():
                item = shared.index_file(db, access, path, size=len(data), content_type=content_type,
                    digest=hashlib.sha256(data).hexdigest(), source=source)
                row = db.get(WorkspaceFile, __import__("uuid").UUID(item["id"]))
                row.live_etag = ops.storage(db).head(shared.object_key(access.workspace_id, path)).get("ETag")
                result.append(read_file(row))
            return {"items": result}
        return ops.execute(db, access, changes, update, request_id=request_id, signature=signature)


def create_directory(db, access, path, *, request_id=None):
    access.editor()
    signature = ops.fingerprint(["mkdir", path])
    with ops.workspace_lock(db, access.workspace_id):
        previous = ops.replay(db, access, request_id, signature)
        if previous is not None:
            return previous
        files, directories, _ = snapshot(db, access)
        validate_destination(path, files, directories)
        if path in files or path in directories:
            raise HTTPException(409, "目标位置已存在同名文件或文件夹，请更换名称")
        parent = str(PurePosixPath(path).parent)
        if parent != "." and parent not in directories:
            raise HTTPException(404, "上级文件夹不存在，请刷新后选择")
        def update():
            db.add(WorkspaceDirectory(workspace_id=access.workspace_id, path=path, created_by=access.user_id))
            return {"kind": "directory", "name": PurePosixPath(path).name, "path": path}
        return ops.execute(db, access, [{"key": shared.directory_marker_key(access.workspace_id, path),
            "data": b"", "content_type": "application/x-directory", "expected": None}], update,
            request_id=request_id, signature=signature)


def unique_name(path, occupied):
    base = PurePosixPath(path)
    for number in range(2, 10002):
        name = f"{base.stem} ({number}){base.suffix}"
        target = str(base.with_name(name))
        if target not in occupied:
            return target
    raise HTTPException(409, "同名项目过多，请输入新名称")


def transfer(db, access, paths, destination="", *, move=False, rename=None, policy="error", resolutions=None,
             expected=None, request_id=None):
    access.editor()
    paths = normalize_selection(paths)
    resolutions, expected = resolutions or {}, expected or {}
    signature = ops.fingerprint(["move" if move else "copy", paths, destination, rename, policy, resolutions, expected])
    with ops.workspace_lock(db, access.workspace_id):
        previous = ops.replay(db, access, request_id, signature)
        if previous is not None:
            return previous
        files, directories, explicit = snapshot(db, access)
        if destination and destination not in directories:
            raise HTTPException(404, "目标文件夹不存在，请刷新后选择")
        occupied = set(files) | directories
        roots, mappings, dir_mappings, conflicts = {}, {}, {}, []
        for path in paths:
            if path not in occupied:
                raise HTTPException(404, "来源项目不存在，请刷新后重新选择")
            current_revision = tree_revision([path], files, directories) if path in directories else ops.file_revision(files[path])
            if path in expected and expected[path] != current_revision:
                raise HTTPException(409, "来源项目已变化，请刷新后重试")
            target = rename or (f"{destination}/" if destination else "") + PurePosixPath(path).name
            validate_destination(target, files, directories)
            if target == path and move:
                continue
            if beneath(target, path) and target != path:
                raise HTTPException(409, "不能复制或移动文件夹到自身内部")
            choice = resolutions.get(target, policy)
            if target in occupied and choice == "keep_both":
                target = unique_name(target, occupied)
            if target in occupied and ((path in directories) != (target in directories)):
                conflicts.append({"path": target, "kind": "type_mismatch", "replaceable": False})
                continue
            if target in occupied and path in directories and choice not in ("replace", "merge"):
                conflicts.append({"path": target, "kind": "directory", "replaceable": True})
                continue
            roots[path] = target
            if target in roots.values() and sum(value == target for value in roots.values()) > 1:
                raise HTTPException(409, "所选项目在目标目录中重名，请分开操作")
            occupied.add(target)
            for origin, row in files.items():
                if not beneath(origin, path):
                    continue
                result = target + origin[len(path):]
                if result in directories:
                    conflicts.append({"path": result, "kind": "type_mismatch", "replaceable": False})
                if result in files:
                    selected = resolutions.get(result, policy)
                    if selected == "keep_both":
                        result = unique_name(result, occupied)
                    elif selected != "replace":
                        conflicts.append({"path": result, "kind": "file", "revision": ops.file_revision(files[result]), "replaceable": True})
                    elif expected.get(result) != ops.file_revision(files[result]):
                        conflicts.append({"path": result, "kind": "file", "revision": ops.file_revision(files[result]), "replaceable": True})
                if result in mappings.values():
                    raise HTTPException(409, "所选项目在目标目录中重名，请分开操作")
                mappings[origin] = result
                occupied.add(result)
            for origin in directories:
                if beneath(origin, path):
                    result = target + origin[len(path):]
                    if result in files:
                        conflicts.append({"path": result, "kind": "type_mismatch", "replaceable": False})
                    dir_mappings[origin] = result
        # Cross-root overlaps may otherwise overwrite a selected source during a batch.
        if any(beneath(target, root) for old, target in roots.items() for root in paths if root != old):
            raise HTTPException(409, "目标与本次来源范围重叠，请分开操作")
        if conflicts:
            raise HTTPException(409, {"message": "目标位置有同名项目，请选择保留两份、替换或取消", "conflicts": conflicts})
        extra = sum(files[old].size_bytes - (files[new].size_bytes if new in files else 0) for old, new in mappings.items())
        check_capacity(db, access, extra if not move else 0)
        store = ops.storage(db)
        changes = [{"key": shared.object_key(access.workspace_id, target), "source": shared.object_key(access.workspace_id, origin),
            "source_revision": ops.file_revision(files[origin]), "expected": ops.file_revision(files[target]) if target in files else None,
            "content_type": files[origin].content_type} for origin, target in mappings.items()]
        # Include all explicit nested markers and an explicit target for implicit directories.
        for origin, target in dir_mappings.items():
            if target not in explicit:
                changes.append({"key": shared.directory_marker_key(access.workspace_id, target), "data": b"",
                    "content_type": "application/x-directory", "expected": None})
        if move:
            changes += [{"key": shared.object_key(access.workspace_id, origin), "delete": True,
                "expected": ops.file_revision(files[origin])} for origin in mappings]
            changes += [{"key": shared.directory_marker_key(access.workspace_id, origin), "delete": True,
                "expected": ops.revision(store.head(shared.directory_marker_key(access.workspace_id, origin)))}
                for origin in dir_mappings if origin in explicit]
        def update():
            for origin, target in mappings.items():
                row = files[origin]
                existing = db.scalar(select(WorkspaceFile).where(WorkspaceFile.workspace_id == access.workspace_id,
                    WorkspaceFile.path == target))
                if existing and existing.id != row.id:
                    _mark_deleted(db, access, existing)
                    for version in list(existing.versions):
                        db.delete(version)
                    existing.path = f"deleted/{existing.id}/{uuid4().hex}"
                    db.flush()
                if move:
                    row.path = target
                    row.updated_at = now()
                    for version in row.versions:
                        version.object_key = shared.object_key(access.workspace_id, target)
                    for attachment in db.scalars(select(ChatAttachment).where(ChatAttachment.resource_file_id == row.id)):
                        attachment.resource_path, attachment.object_key = target, shared.object_key(access.workspace_id, target)
                else:
                    shared.index_file(db, access, target, size=row.size_bytes, content_type=row.content_type,
                        digest=row.content_hash, source=row.source)
            for origin in dir_mappings:
                if move and origin in explicit:
                    db.delete(explicit[origin])
            db.flush()
            shared.refresh_index(db, access, commit=False)
            return {"moved" if move else "copied": paths, "destination": destination, "targets": list(roots.values())}
        return ops.execute(db, access, changes, update, request_id=request_id, signature=signature)
