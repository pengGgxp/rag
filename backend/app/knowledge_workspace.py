"""Synchronize supported knowledge documents with the shared workspace mount."""
import hashlib
import io
import mimetypes
from pathlib import PurePosixPath
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from fastapi import HTTPException
from sqlalchemy import select, update

from .auth import now
from .models import (
    Document,
    DocumentStatus,
    EmbeddingStatus,
    IngestionJob,
    JobState,
    KnowledgeBase,
    KnowledgeBaseFileSnapshot,
    KnowledgeBaseStatus,
    WorkspaceDirectory,
    WorkspaceFile,
)
from .shared_resources import object_key, prefix


def _root_segment(name):
    from .workspace_files import valid_path
    value = name.strip()
    try:
        valid_path(f"知识库/{value}")
    except HTTPException:
        value = "".join("_" if char in '\\/:<>"|?*' or ord(char) < 32 else char for char in value)
        value = value.strip(" .") or "未命名知识库"
    return value


def _file_segment(name):
    value = "".join("_" if char in '\\/:<>"|?*' or ord(char) < 32 else char for char in name)
    value = value.strip(" .")
    if not value or value in (".", ".."):
        value = "未命名文档"
    return value[:255]


def operation_id(workspace_id, request_id=None):
    """Give every workspace mutation a stable UUID for its knowledge snapshot."""
    if request_id:
        try:
            return UUID(str(request_id))
        except (TypeError, ValueError):
            pass
    return uuid5(NAMESPACE_URL, f"knowledge-workspace:{workspace_id}:{request_id or uuid4()}")


def ensure_roots(db, workspace_id, access=None):
    libraries = db.scalars(select(KnowledgeBase).where(
        KnowledgeBase.workspace_id == workspace_id).order_by(KnowledgeBase.created_at, KnowledgeBase.id)
    ).all()
    occupied = {row.workspace_path for row in libraries if row.workspace_path}
    if access:
        from .shared_resources import refresh_index
        refresh_index(db, access, commit=False)
    live_paths = set(db.scalars(select(WorkspaceFile.path).where(
        WorkspaceFile.workspace_id == workspace_id, WorkspaceFile.deleted_at.is_(None))).all())
    live_paths.update(db.scalars(select(WorkspaceDirectory.path).where(
        WorkspaceDirectory.workspace_id == workspace_id)).all())
    for path in tuple(live_paths):
        live_paths.update(str(parent) for parent in PurePosixPath(path).parents if str(parent) != ".")
    for library in libraries:
        if library.workspace_path:
            continue
        root = f"知识库/{_root_segment(library.name)}"
        if root in occupied or any(path == root or path.startswith(root + "/") for path in live_paths):
            base = root
            root = f"{base} ({str(library.id)[:8]})"
            if root in occupied or any(path == root or path.startswith(root + "/") for path in live_paths):
                root = f"{base} ({library.id})"
        library.workspace_path = root
        occupied.add(root)
    db.flush()
    return libraries


def materialize_workspace_roots(db, access, *, refresh=True):
    """Expose every knowledge-base root as a normal workspace directory."""
    from .models import WorkspaceDirectory
    from .shared_resources import backend, directory_marker_key

    libraries = ensure_roots(db, access.workspace_id, access if refresh else None)
    store = backend(db)
    if store is None:
        return libraries
    directories = set(db.scalars(select(WorkspaceDirectory.path).where(
        WorkspaceDirectory.workspace_id == access.workspace_id)).all())
    files = set(db.scalars(select(WorkspaceFile.path).where(
        WorkspaceFile.workspace_id == access.workspace_id,
        WorkspaceFile.deleted_at.is_(None))).all())
    for library in libraries:
        path = library.workspace_path
        if not path or path in files:
            continue
        if path not in directories:
            marker = directory_marker_key(access.workspace_id, path)
            if store.head(marker) is None:
                options = {"create": True} if getattr(store, "conditional_create", False) else {}
                store.put(marker, io.BytesIO(b""), "application/x-directory", **options)
            db.add(WorkspaceDirectory(
                workspace_id=access.workspace_id,
                path=path,
                created_by=access.user_id,
            ))
            directories.add(path)
    db.flush()
    return libraries


def library_for_path(db, workspace_id, path, libraries=None):
    if libraries is None:
        libraries = ensure_roots(db, workspace_id)
    matches = [library for library in libraries if path.startswith(library.workspace_path + "/")]
    return max(matches, key=lambda library: len(library.workspace_path), default=None)


def _supported_path(path):
    from .document_ingestion import SUFFIXES
    return PurePosixPath(path).suffix.lower() in SUFFIXES


def _file_bytes(store, key, filename):
    from .document_ingestion import check_format, inspect_file
    with store.download(key) as stream:
        size, digest = inspect_file(stream)
        check_format(stream, filename)
    return size, digest


def _workspace_documents(db, access, library_ids=None, *, include_restricted=False):
    conditions = [
        KnowledgeBase.workspace_id == access.workspace_id,
        Document.workspace_path.is_not(None),
        Document.is_active.is_(True),
        Document.status != DocumentStatus.DELETED,
    ]
    if not include_restricted:
        conditions.append(access.visible_libraries())
    if library_ids:
        conditions.append(Document.knowledge_base_id.in_(library_ids))
    rows = db.scalars(select(Document).join(KnowledgeBase).where(*conditions)
        .order_by(Document.version_number.desc(), Document.created_at.desc(), Document.id)).all()
    return rows


def prepare_changes(db, access, request_id, paths=None):
    """Keep exact pre-command bytes so replacements retain their cited versions."""
    access.editor()
    request_id = operation_id(access.workspace_id, request_id)
    existing = db.scalar(select(KnowledgeBaseFileSnapshot).where(
        KnowledgeBaseFileSnapshot.workspace_id == access.workspace_id,
        KnowledgeBaseFileSnapshot.request_id == request_id).with_for_update())
    if existing:
        return existing
    from .workspace_mutations import beneath, normalize_selection
    roots = normalize_selection(paths) if paths else None
    store = __import__("app.shared_resources", fromlist=["backend"]).backend(db)
    if store is None:
        raise HTTPException(503, "工作区暂时无法读取，请稍后重试")
    snapshots = []
    try:
        for document in _workspace_documents(db, access, include_restricted=True):
            if roots and not any(beneath(document.workspace_path, root) or
                                 beneath(root, document.workspace_path) for root in roots):
                continue
            key = object_key(access.workspace_id, document.workspace_path)
            metadata = store.head(key)
            if metadata is None:
                if document.object_key != key and store.head(document.object_key):
                    continue
                raise HTTPException(409, "知识库原件已变化，无法留存当前版本，请先同步文件")
            archive_key = (f"workspaces/{access.workspace_id}/knowledge-history/"
                           f"{request_id}/{document.id}")
            if store.head(archive_key) is None:
                from .file_operations import copy_object
                copy_object(store, key, archive_key, etag=metadata.get("ETag"), create=True)
            document.object_key = archive_key
            snapshots.append({
                "document_id": str(document.id),
                "path": document.workspace_path,
                "object_key": key,
                "archive_key": archive_key,
                "etag": metadata.get("ETag"),
                "size_bytes": document.size_bytes,
                "content_hash": document.content_hash,
            })
        row = KnowledgeBaseFileSnapshot(workspace_id=access.workspace_id,
            request_id=request_id, state="prepared", snapshots=snapshots,
            result={"paths": roots or []})
        db.add(row)
        db.commit()
        return row
    except Exception:
        db.rollback()
        for snapshot in snapshots:
            try:
                store.delete(snapshot["archive_key"])
            except Exception:
                pass
        raise


def prepare_if_relevant(db, access, request_id, paths=None):
    """Archive affected indexed originals before a shared-mount write."""
    if paths is None:
        if ensure_roots(db, access.workspace_id, access):
            return prepare_changes(db, access, request_id)
        return None
    from .workspace_mutations import beneath, normalize_selection
    requested = normalize_selection(paths)
    roots = [row.workspace_path for row in ensure_roots(db, access.workspace_id, access) if row.workspace_path]
    relevant = [path for path in requested
        if any(beneath(path, root) or beneath(root, path) for root in roots)]
    if not relevant:
        return None
    return prepare_changes(db, access, request_id, paths=relevant)


def migrate_legacy_documents(db, access, library, store=None):
    """Copy active legacy originals into the shared mount without changing their chunks."""
    ensure_roots(db, access.workspace_id, access)
    store = store or __import__("app.shared_resources", fromlist=["backend"]).backend(db)
    if store is None:
        raise HTTPException(503, "工作区暂时无法读取，请稍后重试")
    from .file_operations import copy_object
    from .workspace_mutations import snapshot, unique_name
    from .workspace_files import valid_path

    documents = db.scalars(select(Document).where(
        Document.knowledge_base_id == library.id,
        Document.status != DocumentStatus.DELETED,
        Document.is_active.is_(True),
    ).order_by(Document.created_at, Document.id).with_for_update()).all()
    files, directories, _ = snapshot(db, access, refresh=False)
    for document in documents:
        if document.workspace_path:
            continue
        segment = _file_segment(document.filename)
        candidate = valid_path(f"{library.workspace_path}/{segment}")
        key = object_key(access.workspace_id, candidate)
        existing = store.head(key)
        if existing:
            size, digest = _file_bytes(store, key, PurePosixPath(candidate).name)
            if digest != document.content_hash:
                candidate = unique_name(candidate, set(files) | directories)
                key = object_key(access.workspace_id, candidate)
                existing = store.head(key)
        if not existing:
            source = store.head(document.object_key)
            if source is None:
                raise HTTPException(409, f"知识库原件「{document.filename}」不可用，无法完成迁移")
            copy_object(store, document.object_key, key, etag=source.get("ETag"), create=True)
            size, digest = _file_bytes(store, key, PurePosixPath(candidate).name)
            if digest != document.content_hash or size != document.size_bytes:
                store.delete(key)
                raise HTTPException(409, f"知识库原件「{document.filename}」校验失败，未完成迁移")
        else:
            size = int(existing.get("Content-Length", existing.get("Size", document.size_bytes)))
            digest = document.content_hash
        content_type = document.content_type or mimetypes.guess_type(candidate)[0] or "application/octet-stream"
        resource = _index_resource(db, access, candidate, size, digest, content_type)
        for version in db.scalars(select(Document).where(
                Document.version_group_id == document.version_group_id)):
            version.workspace_file_id = resource.id
            version.workspace_path = candidate
        document.object_key = key
        document.filename = PurePosixPath(candidate).name
        files[candidate] = resource
    db.commit()


def rename_library_root(db, access, library, name, request_id=None):
    """Keep the visible knowledge directory aligned with a library rename."""
    from .workspace_mutations import snapshot, transfer
    from .workspace_files import valid_path
    ensure_roots(db, access.workspace_id, access)
    old_root = library.workspace_path
    proposed = valid_path(f"知识库/{_root_segment(name)}")
    files, directories, _ = snapshot(db, access)
    occupied = set(files) | directories | {
        row.workspace_path for row in db.scalars(select(KnowledgeBase).where(
            KnowledgeBase.workspace_id == access.workspace_id, KnowledgeBase.id != library.id))
        if row.workspace_path
    }
    target = proposed
    if any(path == proposed or path.startswith(proposed + "/") for path in occupied):
        from .workspace_mutations import unique_name
        target = unique_name(proposed, occupied)
    if old_root == target:
        return
    if old_root in directories:
        transfer(db, access, [old_root], rename=target, move=True, policy="error",
            request_id=str(operation_id(access.workspace_id, request_id)) if request_id else None)
    else:
        library.workspace_path = target
        db.flush()


def _index_resource(db, access, path, size, digest, content_type):
    from .shared_resources import index_file
    item = index_file(db, access, path, size=size, digest=digest, content_type=content_type,
        source="knowledge_base")
    row = db.get(WorkspaceFile, UUID(item["id"]))
    return row


def synchronize(db, access, request_id=None, *, dispatch=None, knowledge_base_id=None,
                content_origin="uploaded", capacity_exclude_key=None, capacity_exclude_keys=()):
    """Reconcile a library subtree against the mounted objects and queue changed documents."""
    from .file_operations import workspace_lock
    with workspace_lock(db, access.workspace_id):
        return _synchronize_locked(db, access, request_id, dispatch=dispatch,
            knowledge_base_id=knowledge_base_id, content_origin=content_origin,
            capacity_exclude_key=capacity_exclude_key, capacity_exclude_keys=capacity_exclude_keys)


def _synchronize_locked(db, access, request_id=None, *, dispatch=None, knowledge_base_id=None,
                        content_origin="uploaded", capacity_exclude_key=None, capacity_exclude_keys=()):
    access = access.revalidate(db) if access.session_hash else access
    access.editor()
    ensure_roots(db, access.workspace_id, access)
    all_libraries = db.scalars(select(KnowledgeBase).where(
        KnowledgeBase.workspace_id == access.workspace_id)).all()
    if knowledge_base_id:
        selected_library = access.library(db, knowledge_base_id, write=True)
        libraries = [selected_library]
    else:
        libraries = all_libraries
    workspace_shared = knowledge_base_id is None
    store = __import__("app.shared_resources", fromlist=["backend"]).backend(db)
    if store is None:
        raise HTTPException(503, "工作区暂时无法读取，请稍后重试")
    for library in libraries:
        migrate_legacy_documents(db, access, library, store)
    snapshot_row = None
    snapshots = {}
    requested_paths = None
    if request_id:
        snapshot_row = db.scalar(select(KnowledgeBaseFileSnapshot).where(
            KnowledgeBaseFileSnapshot.workspace_id == access.workspace_id,
            KnowledgeBaseFileSnapshot.request_id == operation_id(access.workspace_id, request_id)).with_for_update())
        if snapshot_row:
            if snapshot_row.state == "synchronized":
                _dispatch_queued(db, snapshot_row.result.get("queued_jobs", []), dispatch)
                return snapshot_row.result
            snapshots = {item["path"]: item for item in snapshot_row.snapshots}
            requested_paths = snapshot_row.result.get("paths") or None

    mounted = {}
    live_knowledge_paths = set()
    for item in store.iter_objects(prefix(access.workspace_id)):
        key = item.get("Key", "")
        if not key.startswith(prefix(access.workspace_id)):
            continue
        path = key[len(prefix(access.workspace_id)):]
        path_library = library_for_path(db, access.workspace_id, path, all_libraries)
        if path_library and _supported_path(path) and item.get("Type") != "symlink":
            live_knowledge_paths.add(path)
        if requested_paths:
            from .workspace_mutations import beneath
            if not any(beneath(path, root) or beneath(root, path) for root in requested_paths):
                continue
        library = path_library
        if knowledge_base_id and library and library.id != selected_library.id:
            continue
        if not library or not _supported_path(path) or path.rsplit("/", 1)[-1] == ".rag-directory":
            continue
        if item.get("Type") == "symlink":
            continue
        try:
            size, digest = _file_bytes(store, key, PurePosixPath(path).name)
        except HTTPException as error:
            mounted[path] = {"library": library, "key": key, "error": error.detail}
            continue
        content_type = mimetypes.guess_type(path)[0] or "application/octet-stream"
        mounted[path] = {"library": library, "key": key, "size": size,
            "digest": digest, "content_type": content_type, "etag": item.get("ETag")}

    # Refresh the workspace index from mount metadata before attaching stable file IDs.
    from .shared_resources import refresh_index
    refresh_index(db, access, commit=False)
    indexed = {row.path: row for row in db.scalars(select(WorkspaceFile).where(
        WorkspaceFile.workspace_id == access.workspace_id,
        WorkspaceFile.deleted_at.is_(None))).all()}
    all_existing = _workspace_documents(db, access, include_restricted=workspace_shared)
    library_ids = {library.id for library in libraries}
    existing = [document for document in all_existing if document.knowledge_base_id in library_ids]
    by_path = {document.workspace_path: document for document in existing}
    by_file_id = {
        document.workspace_file_id: document for document in all_existing if document.workspace_file_id
    }
    present_paths = set(mounted)
    moved_documents = set()
    retired_documents = set()
    jobs = []
    counts = {"added": 0, "updated": 0, "moved": 0, "deleted": 0, "unchanged": 0, "unsupported": 0}

    for path, item in sorted(mounted.items()):
        resource = indexed.get(path)
        if resource is None and not item.get("error"):
            resource = _index_resource(db, access, path, item["size"], item["digest"], item["content_type"])
            indexed[path] = resource
        current = by_path.get(path)
        if current and resource and current.workspace_file_id and current.workspace_file_id != resource.id:
            archive = snapshots.get(current.workspace_path)
            if archive:
                current.object_key = archive["archive_key"]
            retired_documents.add(current.id)
            current = None
        if current is None and resource:
            current = by_file_id.get(resource.id)
        if current is None and resource:
            retired = db.scalar(select(Document).where(
                Document.knowledge_base_id == item["library"].id,
                Document.workspace_path == path,
                Document.workspace_file_id.is_not(None),
                Document.workspace_file_id != resource.id,
                Document.content_hash == item.get("digest", ""),
                Document.status == DocumentStatus.DELETED,
            ).order_by(Document.version_number.desc(), Document.created_at.desc()).limit(1))
            if retired:
                restored_job = _restore_document(db, retired, item, resource)
                if restored_job is not None:
                    by_path[path] = retired
                    by_file_id[resource.id] = retired
                    existing.append(retired)
                    if restored_job:
                        jobs.append(restored_job)
                        counts["updated"] += 1
                    else:
                        counts["unchanged"] += 1
                    continue
        if item.get("error"):
            counts["unsupported"] += 1
            if current:
                archive = snapshots.get(current.workspace_path)
                if archive:
                    current.object_key = archive["archive_key"]
                elif current.object_key == item["key"] or store.head(current.object_key) is None:
                    raise HTTPException(409, "知识库文件变更前未能留存版本，请重试文件操作")
                _retire_document(db, current)
                retired_documents.add(current.id)
            continue
        if current and current.content_hash == item["digest"]:
            old_path = current.workspace_path
            old_library = db.get(KnowledgeBase, current.knowledge_base_id)
            if old_library.id != item["library"].id and not workspace_shared:
                access.library(db, old_library.id, write=True)
                access.library(db, item["library"].id, write=True)
            for version in db.scalars(select(Document).where(
                    Document.version_group_id == current.version_group_id)):
                version.knowledge_base_id = item["library"].id
                version.workspace_file_id = resource.id
                version.workspace_path = path
                version.filename = PurePosixPath(path).name
                version.content_type = item["content_type"]
                version.size_bytes = item["size"]
            current.object_key = item["key"]
            current.workspace_file_id = resource.id
            current.workspace_path = path
            current.filename = PurePosixPath(path).name
            current.size_bytes = item["size"]
            current.content_type = item["content_type"]
            current.knowledge_base_id = item["library"].id
            if old_path != path:
                moved_documents.add(current.id)
                counts["moved"] += 1
            else:
                counts["unchanged"] += 1
            continue

        if current is None:
            candidates = [document for document in existing
                if document.id not in moved_documents and document.status != DocumentStatus.DELETED
                and document.workspace_path not in live_knowledge_paths
                and document.content_hash == item["digest"]]
            if not candidates:
                candidates = [document for document in all_existing
                    if document.id not in moved_documents and document.status != DocumentStatus.DELETED
                    and document.workspace_path not in live_knowledge_paths
                    and document.content_hash == item["digest"]]
            if len(candidates) == 1:
                current = candidates[0]
                old_library = db.get(KnowledgeBase, current.knowledge_base_id)
                if not workspace_shared:
                    access.library(db, old_library.id, write=True)
                if old_library.id != item["library"].id:
                    if not workspace_shared:
                        access.library(db, item["library"].id, write=True)
                    current.knowledge_base_id = item["library"].id
                versions = db.scalars(select(Document).where(
                    Document.version_group_id == current.version_group_id)).all()
                for version in versions:
                    version.workspace_path = path
                    version.workspace_file_id = resource.id
                    version.filename = PurePosixPath(path).name
                    version.content_type = item["content_type"]
                    version.size_bytes = item["size"]
                    if old_library.id != item["library"].id:
                        version.knowledge_base_id = item["library"].id
                current.object_key = item["key"]
                moved_documents.add(current.id)
                counts["moved"] += 1
                continue

        from .document_ingestion import admit
        library = item["library"]
        previous = current
        archive = snapshots.get(previous.workspace_path) if previous else None
        if previous:
            if previous.knowledge_base_id != library.id:
                if not workspace_shared:
                    access.library(db, previous.knowledge_base_id, write=True)
                    access.library(db, library.id, write=True)
                for version in db.scalars(select(Document).where(
                        Document.version_group_id == previous.version_group_id)):
                    version.knowledge_base_id = library.id
            if archive:
                previous.object_key = archive["archive_key"]
            elif previous.object_key == item["key"] or store.head(previous.object_key) is None:
                raise HTTPException(409, "知识库原件变更前未能留存版本，请重试文件操作")
        document, job = admit(db, access, library.id, PurePosixPath(path).name,
            item["content_type"], item["size"], item["digest"], item["key"],
            version_of=previous.id if previous and previous.status != DocumentStatus.DELETED else None,
            workspace_file_id=resource.id, workspace_path=path, content_origin=content_origin,
            capacity_exclude_key=capacity_exclude_key, capacity_exclude_keys=capacity_exclude_keys,
            workspace_shared=workspace_shared)
        existing.append(document)
        by_path[path] = document
        jobs.append(job.id)
        counts["updated" if previous else "added"] += 1

    for document in existing:
        if document.id in moved_documents or document.id in retired_documents:
            continue
        if document.workspace_path in present_paths:
            continue
        archive = snapshots.get(document.workspace_path)
        if archive:
            document.object_key = archive["archive_key"]
        elif document.object_key == object_key(access.workspace_id, document.workspace_path) or store.head(document.object_key) is None:
            raise HTTPException(409, "知识库文件移除前未能留存版本，请重试文件操作")
        _retire_document(db, document)
        counts["deleted"] += 1
    for document_id in retired_documents:
        document = db.get(Document, document_id)
        if document and document.status != DocumentStatus.DELETED:
            _retire_document(db, document)
            counts["deleted"] += 1

    for library in libraries:
        active = db.scalar(select(Document.id).where(
            Document.knowledge_base_id == library.id,
            Document.status != DocumentStatus.DELETED,
            Document.is_active.is_(True)).limit(1))
        if active is None and library.status != KnowledgeBaseStatus.ARCHIVED:
            library.status = KnowledgeBaseStatus.DRAFT
    result = {"counts": counts, "queued_jobs": [str(job_id) for job_id in jobs],
        "summary": _summary(counts)}
    if snapshot_row:
        snapshot_row.state = "synchronized"
        snapshot_row.result = result
    db.commit()
    _dispatch_queued(db, [str(job_id) for job_id in jobs], dispatch)
    if snapshot_row:
        for snapshot in snapshots.values():
            document = db.get(Document, UUID(snapshot["document_id"]))
            if document and document.object_key == snapshot["archive_key"]:
                continue
            try:
                store.delete(snapshot["archive_key"])
            except Exception:
                pass
    return result


def _retire_document(db, document):
    if document.status == DocumentStatus.DELETED:
        return
    document.status = DocumentStatus.DELETED
    document.is_active = False
    document.error_detail = None
    for job in document.jobs:
        if job.state in (JobState.QUEUED, JobState.RUNNING):
            job.state, job.error_detail = JobState.CANCELLED, "文档已从工作区移除"


def _restore_document(db, document, item, resource):
    chunks = document.chunks
    ready = bool(chunks) and all(
        chunk.embedding_status == EmbeddingStatus.READY and chunk.embedding is not None for chunk in chunks)
    document.status = DocumentStatus.READY if ready else DocumentStatus.QUEUED
    document.is_active = True
    document.object_key = item["key"]
    document.workspace_file_id = resource.id
    document.workspace_path = resource.path
    document.filename = PurePosixPath(resource.path).name
    document.content_type, document.size_bytes = item["content_type"], item["size"]
    document.error_detail = None
    db.execute(update(Document).where(
        Document.version_group_id == document.version_group_id).values(is_active=False))
    document.is_active = True
    job = IngestionJob(document_id=document.id, state=JobState.SUCCEEDED if ready else JobState.QUEUED)
    db.add(job)
    if not ready and chunks:
        for chunk in chunks:
            chunk.embedding = None
            chunk.embedding_status = EmbeddingStatus.PENDING
            chunk.embedding_error = None
    if document.knowledge_base.status != KnowledgeBaseStatus.ARCHIVED:
        document.knowledge_base.status = KnowledgeBaseStatus.READY if ready else KnowledgeBaseStatus.PROCESSING
    db.flush()
    return "" if ready else str(job.id)


def _dispatch_queued(db, identifiers, dispatch=None):
    from .document_ingestion import dispatch_document_job
    for raw_id in identifiers:
        try:
            job = db.get(IngestionJob, UUID(str(raw_id)))
            if job and job.state == JobState.QUEUED:
                if dispatch:
                    dispatch(str(job.id))
                else:
                    dispatch_document_job(job)
        except Exception:
            # A queued job remains durable and the maintenance sweep can dispatch it again.
            continue


def synchronize_if_prepared(db, access, request_id, *, dispatch=None, content_origin="uploaded",
                            capacity_exclude_key=None, capacity_exclude_keys=()):
    if not request_id:
        return None
    try:
        identifier = operation_id(access.workspace_id, request_id)
    except (TypeError, ValueError):
        return None
    row = db.scalar(select(KnowledgeBaseFileSnapshot).where(
        KnowledgeBaseFileSnapshot.workspace_id == access.workspace_id,
        KnowledgeBaseFileSnapshot.request_id == identifier))
    if row is None:
        return None
    return synchronize(db, access, identifier, dispatch=dispatch, content_origin=content_origin,
        capacity_exclude_key=capacity_exclude_key, capacity_exclude_keys=capacity_exclude_keys)


def _summary(counts):
    changes = []
    for key, label in (("added", "新增"), ("updated", "更新"), ("moved", "移动"),
                       ("deleted", "删除"), ("unsupported", "格式不支持")):
        if counts[key]:
            changes.append(f"{label} {counts[key]} 个")
    if not changes:
        return "知识库文件与索引一致"
    return "，".join(changes)


def list_files(db, access, library, path="", *, offset=0, limit=100):
    access.library(db, library.id)
    ensure_roots(db, access.workspace_id, access)
    store = __import__("app.shared_resources", fromlist=["backend"]).backend(db)
    migrate_legacy_documents(db, access, library, store)
    from .workspace_files import valid_path
    root = library.workspace_path + "/"
    target = valid_path(path) if path else library.workspace_path
    if target != library.workspace_path and not target.startswith(root):
        raise HTTPException(422, "路径必须位于当前知识库目录内")
    from .workspace_mutations import snapshot
    files, directories, _ = snapshot(db, access)
    if target and target not in directories and target != library.workspace_path:
        raise HTTPException(404, "知识库文件夹不存在")
    prefix_path = target.rstrip("/") + "/"
    items = []
    for current_path, row in files.items():
        if not current_path.startswith(root):
            continue
        relative = current_path[len(root):]
        if not relative:
            continue
        if target == library.workspace_path or current_path.startswith(prefix_path):
            nested = current_path[len(target.rstrip("/") + "/"):] if target != library.workspace_path else relative
            if "/" in nested:
                continue
            document = db.scalar(select(Document).where(
                Document.knowledge_base_id == library.id,
                Document.workspace_path == current_path,
                Document.is_active.is_(True),
                Document.status != DocumentStatus.DELETED))
            items.append({"kind": "file", "path": current_path, "name": PurePosixPath(current_path).name,
                "size_bytes": row.size_bytes, "content_type": row.content_type,
                "document_id": str(document.id) if document else None,
                "document_status": document.status.value if document else None,
                "indexed": document is not None})
    for directory in directories:
        if directory.startswith(root) and (target == library.workspace_path or directory.startswith(prefix_path)):
            nested = directory[len(target.rstrip("/") + "/"):] if target != library.workspace_path else directory[len(root):]
            if nested and "/" not in nested:
                items.append({"kind": "directory", "path": directory, "name": PurePosixPath(directory).name})
    items.sort(key=lambda item: (item["kind"] != "directory", item["name"].casefold(), item["path"]))
    return {"knowledge_base_id": str(library.id), "path": target, "items": items[offset:offset + limit],
        "offset": offset, "limit": limit, "next_offset": offset + limit if len(items) > offset + limit else None}
