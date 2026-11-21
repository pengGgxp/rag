"""Create a library and its mounted directory in one journaled operation."""
import re

from sqlalchemy import select

from . import file_operations as ops, shared_resources as shared
from .models import KnowledgeBase, WorkspaceDirectory
from .workspace_mutations import snapshot, unique_name, validate_destination


def create_library_directory(db, access, library, finish, *, request_id=None, signature=None):
    account = access.user_id
    with ops.workspace_lock(db, account):
        previous = ops.replay(db, access, request_id, signature)
        if previous is not None:
            return previous
        files, directories, _ = snapshot(db, access)
        occupied = set(files) | directories | set(db.scalars(select(KnowledgeBase.directory_path).where(
            KnowledgeBase.account_id == account, KnowledgeBase.directory_path.is_not(None))))
        name = re.sub(r'[\\/:<>"|?*\x00-\x1f]', '_', library.name).strip().rstrip(' .') or '未命名'
        if name == shared.DIRECTORY_MARKER:
            name += '_'
        path = f'知识库/{name}'
        if path in occupied:
            path = unique_name(path, occupied)
        validate_destination(path, files, directories)
        marker = shared.directory_marker_key(account, path)

        def register():
            db.add(WorkspaceDirectory(account_id=account, path=path, created_by=account, marker_key=marker))
            library.directory_path = path
            db.add(library)
            db.flush()
            return finish()

        return ops.execute(db, access, [{'key': marker, 'expected': None, 'data': b'',
            'content_type': 'application/x-directory'}], register, request_id=request_id, signature=signature)


def publish_upload(db, access, library, filename, content_type, content, digest, request=None, *, upload_row=None):
    from uuid import UUID
    from .document_ingestion import admit
    from .models import Document, IngestionJob
    from .storage_billing import check_capacity
    from .workspace_files import valid_path

    with ops.workspace_lock(db, access.user_id):
        if upload_row is not None:
            receipt_id = f"publish-upload:{upload_row.id}"
            previous = ops.replay(db, access, receipt_id, digest)
            if previous is not None:
                return db.get(Document, UUID(previous["document"])), db.get(IngestionJob, UUID(previous["job"]))
        relative = valid_path(filename)
        path = valid_path(f"{library.directory_path}/{relative}")
        files, directories, _ = snapshot(db, access)
        occupied = set(files) | directories
        if path in occupied:
            path = unique_name(path, occupied)
        validate_destination(path, files, directories)
        key = shared.object_key(access.user_id, path)
        check_capacity(db, access.user_id, len(content), exclude_key=upload_row.object_key if upload_row else None)

        def register():
            resource = shared.index_file(db, access, path, key=key, size=len(content),
                digest=digest, content_type=content_type, source="upload")
            if upload_row is not None:
                upload_row.state = "completed"
                db.flush()
            document, job = admit(db, access, library.id, path.rsplit("/", 1)[-1], content_type,
                len(content), digest, key, request=request, personal_file_id=UUID(resource["id"]))
            if upload_row is not None:
                upload_row.document_id = document.id
            return {"document": str(document.id), "job": str(job.id)}

        result = ops.execute(db, access, [{"key": key, "expected": None, "data": content,
            "content_type": content_type}], register, request_id=receipt_id if upload_row else None, signature=digest)
        return db.get(Document, UUID(result["document"])), db.get(IngestionJob, UUID(result["job"]))
