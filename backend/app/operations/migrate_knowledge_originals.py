"""在离线数据库和对象存储副本上校验并迁移知识库原件。"""
import argparse
import json
from collections import Counter
from pathlib import Path
from uuid import UUID

from sqlalchemy import func, select

from app.infra.db import SessionLocal
from app.files.operations import copy_object
from app.files.integrity import object_fingerprint as _fingerprint
from ..knowledge.originals import original_key
from ..models import (ChatMessage, Chunk, Conversation, Document, DocumentStatus, DocumentUpload, EmbeddingStatus,
                     IngestionJob, JobState, KnowledgeBase, User)
from app.infra.storage_backend import current_storage


class MigrationIssue(Exception):
    pass


def verify(store, key, expected):
    head = store.head(key)
    if head is None:
        raise MigrationIssue("missing_original")
    if _fingerprint(store, key) != expected:
        raise MigrationIssue("checksum_mismatch")
    return head


def ensure_copy(store, source, target, expected):
    before = verify(store, source, expected)
    if source != target and store.head(target) is None:
        copy_object(store, source, target, etag=before.get("ETag"), create=True)
    verify(store, target, expected)


def build_group(db, store, library, rows):
    if db.get(User, library.account_id) is None or library.owner_id != library.account_id:
        raise MigrationIssue("unclear_account")
    active = [row for row in rows if row.is_active]
    if len(active) == 1:
        current = active[0]
    elif not active and len(rows) == 1 and rows[0].status == DocumentStatus.DELETED:
        current = rows[0]
    else:
        raise MigrationIssue("ambiguous_current_original")
    files = []
    for row in rows:
        key = row.object_key
        backup = original_key(library.account_id, library.id, row.id, row.filename).replace(
            "/knowledge-originals/", "/knowledge-migration-backup/", 1)
        target = original_key(library.account_id, library.id, row.id, row.filename,
                              deleted=row.status == DocumentStatus.DELETED) if row is current else backup
        expected = (row.size_bytes, row.content_hash)
        verify(store, key, expected)
        for candidate in {backup, target} - {key}:
            if store.head(candidate) is not None:
                verify(store, candidate, expected)
        files.append({"document_id": str(row.id), "source": key, "target": target,
                      "backup": backup if key != target or row is not current else None,
                      "size": row.size_bytes, "sha256": row.content_hash, "current": row is current})
    vectors = db.scalar(select(func.count(Chunk.id)).where(Chunk.document_id == current.id,
        Chunk.embedding_status == EmbeddingStatus.READY, Chunk.embedding.is_not(None)))
    return current, files, vectors


def citation_inventory(db, accounts, *, execute):
    """Keep the evidence digest before a later replacement removes the old chunks."""
    from ..identity.auth import Access
    from ..knowledge.originals import resolve_citation

    counts = {account: Counter() for account in accounts}
    messages = db.execute(select(ChatMessage, Conversation.account_id).join(Conversation)
        .where(ChatMessage.role == "assistant").execution_options(yield_per=200))
    for message, account in messages:
        if account not in counts:
            continue
        updated = []
        for item in message.citations or []:
            updated.append(item)
            if not isinstance(item, dict) or not item.get("chunk_id"):
                continue
            try:
                chunk_id = UUID(str(item["chunk_id"]))
            except ValueError:
                counts[account]["unknown"] += 1
                continue
            result = resolve_citation(db, Access(account, "knowledge-originals-migration"), item, chunk_id)
            counts[account][result["state"]] += 1
            if execute and result["state"] == "current" and not item.get("original_hash"):
                document = db.get(Document, UUID(result["document_id"]))
                updated[-1] = {**item, "original_hash": document.content_hash}
        if execute and updated != message.citations:
            message.citations = updated
    if execute:
        db.commit()
    return {str(account): dict(counter) for account, counter in counts.items()}


def migrate(db, store, *, execute=False, offline=False):
    if execute and not offline:
        raise ValueError("使用 --offline 确认 API 与 Worker 已停止，并已完成备份")
    active_jobs = db.scalar(select(func.count(IngestionJob.id)).where(
        IngestionJob.state.in_((JobState.QUEUED, JobState.RUNNING))))
    active_uploads = db.scalar(select(func.count(DocumentUpload.id)).where(
        DocumentUpload.state.in_(("creating", "uploading", "completing", "verifying"))))
    if execute and (active_uploads or active_jobs):
        raise ValueError(f"请先收尾上传与入库任务。上传 {active_uploads}，入库 {active_jobs}")
    groups = {}
    for row, library in db.execute(select(Document, KnowledgeBase).join(KnowledgeBase)
                                   .order_by(KnowledgeBase.account_id, Document.version_group_id, Document.id)):
        groups.setdefault((library.id, row.version_group_id), (library, []))[1].append(row)
    accounts = {library.account_id for library, _ in groups.values()}
    def document_bytes():
        usage = dict(db.execute(select(KnowledgeBase.account_id, func.sum(Document.size_bytes))
            .join(Document).where(Document.is_active.is_(True)).group_by(KnowledgeBase.account_id)).all())
        return {str(account): usage.get(account, 0) for account in accounts}

    report = {"executed": execute, "active_uploads": active_uploads, "active_jobs": active_jobs,
              "document_bytes_before": document_bytes(),
              "citations_before": citation_inventory(db, accounts, execute=execute),
              "groups": [], "issues": [], "current_originals": 0, "current_bytes": 0, "current_vectors": 0}
    for (_, group_id), (library, rows) in groups.items():
        try:
            current, files, vectors = build_group(db, store, library, rows)
            if execute:
                for item in files:
                    expected = (item["size"], item["sha256"])
                    if item["backup"]:
                        ensure_copy(store, item["source"], item["backup"], expected)
                    ensure_copy(store, item["source"], item["target"], expected)
                for row, item in zip(rows, files, strict=True):
                    row.object_key = item["target"]
                    if row is not current:
                        # Keep reference identities and digests, and purge live historical content.
                        row.is_active = False
                        row.chunks.clear()
                        row.parsed_text = row.parsing_metadata = row.parser_name = None
                        row.status = DocumentStatus.DELETED
                current.is_active = True
                db.commit()
            report["groups"].append({"account_id": str(library.account_id), "library_id": str(library.id),
                                     "group_id": str(group_id), "files": files, "current_vectors": vectors})
            report["current_originals"] += 1
            report["current_bytes"] += sum(item["size"] for item in files if item["current"])
            report["current_vectors"] += vectors
        except MigrationIssue as exc:
            db.rollback()
            report["issues"].append({"group_id": str(group_id), "library_id": str(library.id),
                                     "documents": [str(row.id) for row in rows], "reason": str(exc)})
        except Exception as exc:
            db.rollback()
            report["issues"].append({"group_id": str(group_id), "library_id": str(library.id),
                                     "documents": [str(row.id) for row in rows], "reason": type(exc).__name__})
    report["document_bytes_after"] = document_bytes()
    report["citations_after"] = citation_inventory(db, accounts, execute=False)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    # Reserve the output path before executing; every run gets its own manifest.
    with args.report.open("x", encoding="utf-8") as output, SessionLocal() as db:
        report = migrate(db, current_storage(db), execute=args.execute, offline=args.offline)
        json.dump(report, output, ensure_ascii=False, indent=2)
        output.write("\n")
    return 1 if report["issues"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
