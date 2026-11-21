"""Reconcile current files in a personal mount with searchable documents."""
from collections import Counter, defaultdict
from pathlib import PurePosixPath
from uuid import uuid4

from sqlalchemy import select

from . import file_operations as ops, shared_resources as shared
from .document_ingestion import SUFFIXES, admit, check_format, inspect_file
from .models import Document, DocumentStatus, IngestionJob, JobState, KnowledgeBase, KnowledgeBaseStatus, WorkspaceFile


def invalidate(db, doc):
    for job in db.scalars(select(IngestionJob).where(IngestionJob.document_id == doc.id,
            IngestionJob.state.in_((JobState.QUEUED, JobState.RUNNING))).with_for_update()):
        job.state = JobState.CANCELLED
        job.submission_pending = False
    doc.chunks.clear()
    doc.parsed_text = doc.parser_name = doc.parsing_metadata = doc.error_detail = None


def reconcile(db, access):
    with ops.workspace_lock(db, access.user_id):
        libraries = {lib.directory_path: lib for lib in db.scalars(select(KnowledgeBase).where(
            KnowledgeBase.account_id == access.user_id, KnowledgeBase.directory_path.is_not(None),
            KnowledgeBase.status != KnowledgeBaseStatus.ARCHIVED))}
        report = {'added': 0, 'moved': 0, 'updated': 0, 'deleted': 0, 'queued_jobs': [], 'failures': []}
        if not libraries:
            return report
        store = ops.storage(db)
        shared.refresh_index(db, access, path_prefix='知识库/', commit=False)
        docs = list(db.scalars(select(Document).where(Document.knowledge_base_id.in_([lib.id for lib in libraries.values()]),
            Document.personal_file_id.is_not(None), Document.status != DocumentStatus.DELETED).with_for_update()))
        by_file = {doc.personal_file_id: doc for doc in docs}
        files = []
        for row in db.scalars(select(WorkspaceFile).where(WorkspaceFile.owner_user_id == access.user_id,
                WorkspaceFile.deleted_at.is_(None), WorkspaceFile.path.startswith('知识库/'))):
            path = PurePosixPath(row.path)
            library = libraries.get('/'.join(path.parts[:2]))
            if not library or path.suffix.lower() not in SUFFIXES:
                continue
            key = shared.object_key(access.user_id, row.path)
            if not row.content_hash:
                from fastapi import HTTPException
                with store.download(key) as body:
                    try:
                        size, digest = inspect_file(body)
                        check_format(body, path.name)
                    except HTTPException as error:
                        report['failures'].append({'path': row.path, 'message': error.detail})
                        continue
                head = store.head(key)
                if not head or head.get('ETag') != row.live_etag:
                    raise RuntimeError('文件同步期间发生变化')
                row.content_hash = digest
            files.append((row, key, library))
        present = {row.id for row, _, _ in files}
        missing = [doc for doc in docs if doc.personal_file_id not in present]
        candidates = defaultdict(list)
        for doc in missing:
            candidates[doc.content_hash].append(doc)
        arrivals = Counter(row.content_hash for row, _, _ in files if row.id not in by_file)
        # Release old unique object keys before path swaps and recreation at the same path.
        prior_keys = {doc.id: doc.object_key for doc in docs}
        intended = {row.id: key for row, key, _ in files}
        for doc in docs:
            if intended.get(doc.personal_file_id) != doc.object_key:
                doc.object_key = f'detached/{doc.id}/{uuid4().hex}'
        db.flush()
        for row, key, lib in files:
            doc = by_file.get(row.id)
            matches = candidates[row.content_hash]
            if doc is None and arrivals[row.content_hash] == 1 and len(matches) == 1:
                doc = matches[0]
                missing.remove(doc)
            if doc is None:
                doc, job = admit(db, access, lib.id, PurePosixPath(row.path).name, row.content_type,
                    row.size_bytes, row.content_hash, key, personal_file_id=row.id)
                report['added'] += 1
                report['queued_jobs'].append(str(job.id))
                continue
            doc.personal_file_id, doc.object_key, doc.knowledge_base_id = row.id, key, lib.id
            doc.filename = PurePosixPath(row.path).name
            if prior_keys[doc.id] != key:
                report['moved'] += 1
            if doc.content_hash != row.content_hash:
                invalidate(db, doc)
                doc.content_hash, doc.size_bytes, doc.content_type = row.content_hash, row.size_bytes, row.content_type
                doc.status = DocumentStatus.QUEUED
                job = IngestionJob(document_id=doc.id, submission_pending=True)
                db.add(job)
                db.flush()
                report['updated'] += 1
                report['queued_jobs'].append(str(job.id))
        for doc in missing:
            invalidate(db, doc)
            doc.status, doc.is_active, doc.personal_file_id = DocumentStatus.DELETED, False, None
            report['deleted'] += 1
        db.commit()
        return report
