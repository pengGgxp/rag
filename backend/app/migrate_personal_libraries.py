"""Offline migration of current originals. Run against restored backups first."""
from collections import defaultdict
from pathlib import PurePosixPath
from uuid import UUID

from sqlalchemy import select

from . import file_operations as ops, shared_resources as shared
from .auth import Access
from .models import Document, DocumentStatus, KnowledgeBase, JobState
from .personal_library_files import create_library_directory
from .workspace_mutations import snapshot, unique_name
from .workspace_files import valid_path


def migrate(db, *, apply=False):
    store = ops.storage(db)
    report = {'problems': [], 'current': [], 'historical': [], 'paths': {}}
    libraries = list(db.scalars(select(KnowledgeBase).order_by(KnowledgeBase.id)))
    for lib in libraries:
        groups = defaultdict(list)
        for doc in lib.documents:
            if doc.personal_file_id is None and not doc.object_key.startswith('detached/'):
                groups[doc.version_group_id].append(doc)
        for group, docs in groups.items():
            already_mounted = any(d.personal_file_id and d.version_group_id == group for d in lib.documents)
            active = [doc for doc in docs if doc.is_active and doc.status != DocumentStatus.DELETED]
            if not already_mounted and len(active) != 1 and any(doc.status != DocumentStatus.DELETED for doc in docs):
                report['problems'].append({'group': str(group), 'error': '无法确定当前原件'})
                continue
            for doc in docs:
                if any(job.state in (JobState.RUNNING, JobState.QUEUED) for job in doc.jobs):
                    report['problems'].append({'document': str(doc.id), 'error': '文档仍在处理'})
                head = store.head(doc.object_key)
                if not head or ops.digest_object(store, doc.object_key) != doc.content_hash:
                    report['problems'].append({'document': str(doc.id), 'error': '原件缺失或摘要不符'})
                report['current' if doc in active else 'historical'].append(str(doc.id))
    if not apply or report['problems']:
        return report
    for lib in libraries:
        if lib.directory_path is None:
            create_library_directory(db, Access(lib.account_id, ''), lib, lambda: {})
    for identifier in report['current']:
        doc = db.get(Document, UUID(identifier))
        lib = doc.knowledge_base
        access = Access(lib.account_id, '')
        with ops.workspace_lock(db, access.user_id):
            files, directories, _ = snapshot(db, access)
            path = valid_path(f'{lib.directory_path}/{PurePosixPath(doc.filename).name}')
            if path in files or path in directories:
                path = unique_name(path, set(files) | directories)
            target = shared.object_key(access.user_id, path)
            source = doc.object_key
            def register():
                if ops.digest_object(store, target) != doc.content_hash:
                    raise ValueError('迁移目标校验失败')
                row = shared.index_file(db, access, path, key=target, size=doc.size_bytes,
                    content_type=doc.content_type, digest=doc.content_hash, source='upload')
                doc.personal_file_id, doc.object_key = UUID(row['id']), target
                return {'path': path}
            ops.execute(db, access, [{'key': target, 'source': source, 'expected': None,
                'source_revision': ops.revision(store.head(source))}], register)
            report['paths'][identifier] = path
    # Keep historical source bytes in the migration backup, never in the live mount.
    for identifier in report['historical']:
        doc = db.get(Document, UUID(identifier))
        owner = doc.knowledge_base.account_id
        source = doc.object_key
        backup = f'accounts/{owner}/migration-backups/138/{doc.id}/{doc.content_hash}'
        if store.head(backup) is None:
            ops.copy_object(store, source, backup, etag=store.head(source)['ETag'], create=True)
        if ops.digest_object(store, backup) != doc.content_hash:
            raise ValueError('历史原件备份校验失败')
        # Preserve FK targets for audit and old citations, without a readable historical original.
        doc.object_key = f'detached/{doc.id}'
        doc.status, doc.is_active = DocumentStatus.DELETED, False
        doc.parsed_text = doc.parsing_metadata = None
        doc.chunks.clear()
        db.commit()
    return report
