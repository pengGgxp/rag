"""Managed, private read-only files exposed at the resource directory's skill-packages.

The database remains the publishing authority. Only a serialized command worker
writes each computer's private object prefix; the sandbox mounts that prefix
read-only, separately from the workspace's writable shared resources.
"""
import base64
import hashlib
import io
import json
import re
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import joinedload

from .models import AgentSkill, SiteSkill
from .skill_files import bundle_files, safe_path, validate_files

DIRECTORY = "skill-packages"
MOUNT = "/home/user/workspace/skill-packages"
COMMAND_PATH = re.compile(r"(?<![\w.-])skill-packages/"
    r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"
    r"(?=/|$|[\s\"'`;|&<>])")


def required_ids(arguments):
    command = arguments.get("preview", {}).get("command", arguments.get("command", ""))
    paths = command + " " + arguments.get("file_operation", {}).get("path", "")
    # Shell expansions are resolved inside the isolated execution environment.
    # They cannot be inferred as authorization IDs by scanning command text.
    return set(arguments.get("skill_ids", ())) | {str(UUID(value)) for value in COMMAND_PATH.findall(paths)}


def hidden(path):
    return path.split("/", 1)[0].casefold() == DIRECTORY


def visible_rows():
    from .models import WorkspaceFile
    return (~WorkspaceFile.path.ilike(DIRECTORY),
            ~WorkspaceFile.path.ilike(DIRECTORY + "/%"))


def prefix(workspace_id, user_id, computer_id):
    # Keep the private mount within the existing workspace authorization tree,
    # alongside (never inside) resource-directory, with bounded UUID components.
    return f"workspaces/{UUID(str(workspace_id))}/skills/{UUID(str(user_id)).hex}/{UUID(str(computer_id)).hex}/"


def current_packages(db, access):
    from .personal_skills import package_review
    from .site_skill_packages import review
    packages, versions = {}, {}
    queries = (select(SiteSkill).options(joinedload(SiteSkill.files)).where(SiteSkill.enabled.is_(True)),
        select(AgentSkill).options(joinedload(AgentSkill.files)).where(
            AgentSkill.enabled.is_(True), AgentSkill.owner_id == access.user_id,
            AgentSkill.workspace_id == access.workspace_id))
    for query in queries:
        for row in db.scalars(query).unique():
            files = bundle_files(row)
            checked = (package_review if isinstance(row, AgentSkill) else review)(files)
            if not checked["can_enable"] or (isinstance(row, AgentSkill)
                    and checked["has_limitations"] and not row.limitations_acknowledged):
                continue
            identifier = str(row.id)
            if identifier in packages:
                raise HTTPException(409, "Skill 标识冲突，请重新加载后再试")
            packages[identifier] = {path: base64.b64encode(data).decode() for path, data in files.items()}
            versions[identifier] = row.version
    return packages, versions


def authorized(db, access, identifier):
    try:
        identifier = UUID(identifier)
    except (ValueError, TypeError, AttributeError):
        return False
    platform = db.get(SiteSkill, identifier, populate_existing=True)
    private = db.get(AgentSkill, identifier, populate_existing=True)
    if platform is not None and platform.enabled:
        if private is not None and private.enabled and private.owner_id == access.user_id and private.workspace_id == access.workspace_id:
            raise HTTPException(409, "Skill 标识冲突，请重新加载后再试")
        return True
    return bool(private is not None and private.enabled and private.owner_id == access.user_id
        and private.workspace_id == access.workspace_id)


def synchronize(db, computer, access, packages, versions, required=()):
    from .cloud_computer import mount_storage
    root = prefix(computer.workspace_id, computer.user_id, computer.id)
    if computer.snapshot.get("skill_mount", {}).get("prefix") != root:
        raise HTTPException(409, "请先结束当前云电脑，再使用 Skill")
    files, active_versions = {}, {}
    for identifier, encoded in packages.items():
        if not authorized(db, access, identifier):
            if identifier in required:
                raise HTTPException(404, "指定的方法不存在或当前不可用")
            continue
        try:
            decoded = validate_files({safe_path(path): base64.b64decode(data, validate=True)
                for path, data in encoded.items()})
        except (ValueError, TypeError):
            raise HTTPException(422, "Skill 文件无效，请重新上传后再试") from None
        files.update({f"{identifier}/{path}": data for path, data in decoded.items()})
        active_versions[identifier] = versions.get(identifier)
    if set(required) - set(active_versions):
        raise HTTPException(404, "指定的方法不存在或当前不可用")
    manifest = {"versions": active_versions, "files": {path: hashlib.sha256(data).hexdigest()
        for path, data in sorted(files.items())}}
    metadata = json.dumps(manifest, sort_keys=True).encode()
    digest = hashlib.sha256(metadata).hexdigest()
    if computer.manifest.get("skill_directory_digest") == digest:
        return
    storage = mount_storage(db, computer)
    computer.manifest = {key: value for key, value in computer.manifest.items()
        if key != "skill_directory_digest"}
    computer.manifest = {**computer.manifest, "skill_directory_dirty": True}
    db.commit()
    # Fail closed on partial publication. The worker never starts a command until
    # every file and the final manifest are stored, and only then commits the digest.
    for path, data in files.items():
        storage.put(root + path, io.BytesIO(data), "application/octet-stream")
    wanted = set(files) | {".directory.json"}
    for item in storage.iter_objects(root):
        key = item["Key"]
        if not key.startswith(root):
            raise HTTPException(503, "Skill 目录暂时不可用，请稍后重试")
        if key[len(root):] not in wanted:
            storage.delete(key)
    storage.put(root + ".directory.json", io.BytesIO(metadata), "application/json")
    if any(not authorized(db, access, identifier) for identifier in active_versions):
        raise HTTPException(409, "方法状态已变化，请重新发起操作")
    computer.manifest = {**computer.manifest, "skill_directory_digest": digest,
        "skill_versions": active_versions}
    db.commit()


def cleanup(db, computer):
    """Remove only a terminated computer's disposable private publication.

    Keep the dirty marker until every delete succeeds so maintenance can retry
    without delaying destruction, resource persistence or billing settlement.
    """
    if not computer.ended_at or not computer.manifest.get("skill_directory_dirty"):
        return
    from .cloud_computer import mount_storage
    root = prefix(computer.workspace_id, computer.user_id, computer.id)
    if computer.snapshot.get("skill_mount", {}).get("prefix") != root:
        raise HTTPException(503, "Skill 目录暂时不可用")
    storage = mount_storage(db, computer)
    for item in storage.iter_objects(root):
        if not item["Key"].startswith(root):
            raise HTTPException(503, "Skill 目录暂时不可用")
        storage.delete(item["Key"])
    computer.manifest = {key: value for key, value in computer.manifest.items()
        if not key.startswith("skill_directory_")}
    db.commit()
