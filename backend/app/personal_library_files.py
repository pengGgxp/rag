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


def publish_upload(db, access, library, filename, content_type, content, digest, request=None, *, upload_row=None, replace_id=None):
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
        previous_doc = None
        expected = None
        if replace_id:
            from fastapi import HTTPException
            from .document_ingestion import latest_document_job
            previous_doc = db.get(Document, replace_id)
            if not previous_doc or previous_doc.knowledge_base_id != library.id or not previous_doc.personal_file_id:
                raise HTTPException(404, "未找到要替换的个人原件")
            path = previous_doc.relative_path
            row = files.get(path)
            if not row or row.id != previous_doc.personal_file_id:
                raise HTTPException(409, "原件已移动，请刷新后重试")
            expected = ops.file_revision(row)
            if previous_doc.content_hash == digest and ops.digest_object(ops.storage(db), previous_doc.object_key) == digest:
                return previous_doc, latest_document_job(db, previous_doc)
        elif path in occupied:
            path = unique_name(path, occupied)
        validate_destination(path, files, directories)
        key = shared.object_key(access.user_id, path)
        check_capacity(db, access.user_id, len(content) - (previous_doc.size_bytes if previous_doc else 0), exclude_key=upload_row.object_key if upload_row else None)

        def register():
            resource = shared.index_file(db, access, path, key=key, size=len(content),
                digest=digest, content_type=content_type, source="upload")
            if upload_row is not None:
                upload_row.state = "completed"
                db.flush()
            if previous_doc is not None:
                from .personal_library_sync import invalidate
                from .models import DocumentStatus
                document = db.get(Document, previous_doc.id, with_for_update=True, populate_existing=True)
                invalidate(db, document)
                document.status, document.content_hash = DocumentStatus.QUEUED, digest
                document.size_bytes, document.content_type = len(content), content_type
                job = IngestionJob(document_id=document.id, submission_pending=True)
                db.add(job)
                db.flush()
            else:
                document, job = admit(db, access, library.id, path.rsplit("/", 1)[-1], content_type,
                    len(content), digest, key, request=request, personal_file_id=UUID(resource["id"]))
            if upload_row is not None:
                upload_row.document_id = document.id
            return {"document": str(document.id), "job": str(job.id)}

        result = ops.execute(db, access, [{"key": key, "expected": expected, "data": content,
            "content_type": content_type}], register, request_id=receipt_id if upload_row else None, signature=digest)
        return db.get(Document, UUID(result["document"])), db.get(IngestionJob, UUID(result["job"]))
