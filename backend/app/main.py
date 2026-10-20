import hashlib
import io
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import UUID, uuid4

import redis
from fastapi import Depends, FastAPI, File, HTTPException, UploadFile, status
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import func, or_, select, text
from sqlalchemy.orm import Session

from .config import get_settings
from .db import SessionLocal, engine, init_db
from .embedding import OpenAICompatibleEmbeddingProvider
from .models import (
    Document,
    DocumentStatus,
    Chunk,
    IngestionJob,
    JobState,
    KnowledgeBase,
    KnowledgeBaseStatus,
)
from .schemas import (
    DocumentRead,
    ChunkRead,
    JobRead,
    KnowledgeBaseCreate,
    KnowledgeBaseRead,
    SearchResult,
)
from .storage import ensure_bucket, upload_fileobj
from .tasks import process_document


def get_db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    ensure_bucket()
    yield


app = FastAPI(title="RAG Platform API", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health():
    settings = get_settings()
    db_ok = False
    redis_ok = False
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        db_ok = True
    except Exception:
        pass
    try:
        redis_ok = bool(redis.Redis.from_url(settings.redis_url).ping())
    except Exception:
        pass
    overall = db_ok and redis_ok
    return {
        "status": "ok" if overall else "degraded",
        "database": "ok" if db_ok else "error",
        "redis": "ok" if redis_ok else "error",
        "embedding": OpenAICompatibleEmbeddingProvider().status.__dict__,
    }


@app.post("/knowledge-bases", response_model=KnowledgeBaseRead, status_code=status.HTTP_201_CREATED)
def create_knowledge_base(payload: KnowledgeBaseCreate, db: Session = Depends(get_db)):
    knowledge_base = KnowledgeBase(name=payload.name, description=payload.description)
    db.add(knowledge_base)
    db.commit()
    db.refresh(knowledge_base)
    return KnowledgeBaseRead.model_validate(knowledge_base)


@app.get("/knowledge-bases", response_model=list[KnowledgeBaseRead])
def list_knowledge_bases(db: Session = Depends(get_db)):
    rows = db.execute(
        select(KnowledgeBase, func.count(Document.id))
        .outerjoin(Document, Document.knowledge_base_id == KnowledgeBase.id)
        .group_by(KnowledgeBase.id)
        .order_by(KnowledgeBase.created_at.desc())
    ).all()
    return [
        KnowledgeBaseRead.model_validate(kb).model_copy(update={"document_count": count})
        for kb, count in rows
    ]


@app.post(
    "/knowledge-bases/{knowledge_base_id}/documents",
    response_model=DocumentRead,
    status_code=status.HTTP_201_CREATED,
)
def upload_document(
    knowledge_base_id: UUID,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    knowledge_base = db.get(KnowledgeBase, knowledge_base_id)
    if knowledge_base is None:
        raise HTTPException(status_code=404, detail="Knowledge base not found")
    allowed = {
        "application/pdf",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "text/markdown",
        "text/plain",
        "text/html",
        "text/csv",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    }
    allowed_suffixes = {".pdf", ".docx", ".md", ".txt", ".html", ".htm", ".csv", ".xlsx"}
    suffix = Path(file.filename or "").suffix.lower()
    if file.content_type not in allowed and suffix not in allowed_suffixes:
        raise HTTPException(status_code=415, detail="Unsupported document type")

    content = file.file.read()
    if not content:
        raise HTTPException(status_code=400, detail="Uploaded file is empty")
    if len(content) > 100 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="File exceeds 100 MB limit")

    digest = hashlib.sha256(content).hexdigest()
    existing = db.scalar(
        select(Document).where(
            Document.knowledge_base_id == knowledge_base_id,
            Document.content_hash == digest,
            Document.status != DocumentStatus.DELETED,
        )
    )
    if existing is not None:
        raise HTTPException(status_code=409, detail="This document already exists")

    object_key = f"{knowledge_base_id}/{uuid4()}-{file.filename}"
    upload_fileobj(
        fileobj=io.BytesIO(content),
        object_key=object_key,
        content_type=file.content_type,
    )
    document = Document(
        knowledge_base_id=knowledge_base_id,
        filename=file.filename or "untitled",
        content_type=file.content_type,
        size_bytes=len(content),
        object_key=object_key,
        content_hash=digest,
    )
    db.add(document)
    db.flush()
    job = IngestionJob(document_id=document.id)
    db.add(job)
    knowledge_base.status = KnowledgeBaseStatus.PROCESSING
    db.commit()
    db.refresh(document)
    process_document.delay(str(job.id))
    return DocumentRead.model_validate(document).model_copy(
        update={"job": JobRead.model_validate(job)}
    )


@app.get("/knowledge-bases/{knowledge_base_id}/documents", response_model=list[DocumentRead])
def list_documents(knowledge_base_id: UUID, db: Session = Depends(get_db)):
    if db.get(KnowledgeBase, knowledge_base_id) is None:
        raise HTTPException(status_code=404, detail="Knowledge base not found")
    documents = db.scalars(
        select(Document)
        .where(Document.knowledge_base_id == knowledge_base_id)
        .order_by(Document.created_at.desc())
    ).all()
    result = []
    for document in documents:
        job = db.scalar(
            select(IngestionJob)
            .where(IngestionJob.document_id == document.id)
            .order_by(IngestionJob.created_at.desc())
        )
        result.append(
            DocumentRead.model_validate(document).model_copy(
                update={"job": JobRead.model_validate(job) if job else None}
            )
        )
    return result


@app.get("/jobs/{job_id}", response_model=JobRead)
def get_job(job_id: UUID, db: Session = Depends(get_db)):
    job = db.get(IngestionJob, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@app.get("/documents/{document_id}/chunks", response_model=list[ChunkRead])
def list_chunks(document_id: UUID, db: Session = Depends(get_db)):
    document = db.get(Document, document_id)
    if document is None:
        raise HTTPException(status_code=404, detail="Document not found")
    return db.scalars(
        select(Chunk)
        .where(Chunk.document_id == document_id)
        .order_by(Chunk.ordinal)
    ).all()


@app.post("/knowledge-bases/{knowledge_base_id}/search", response_model=list[SearchResult])
def search_knowledge_base(
    knowledge_base_id: UUID,
    query: str,
    db: Session = Depends(get_db),
):
    knowledge_base = db.get(KnowledgeBase, knowledge_base_id)
    if knowledge_base is None:
        raise HTTPException(status_code=404, detail="Knowledge base not found")
    terms = [term for term in query.split() if term]
    if not terms:
        return []
    conditions = [Chunk.text.ilike(f"%{term}%") for term in terms]
    rows = db.execute(
        select(Chunk, Document.filename)
        .join(Document, Document.id == Chunk.document_id)
        .where(
            Document.knowledge_base_id == knowledge_base_id,
            Document.status == DocumentStatus.READY,
            or_(*conditions),
        )
        .limit(20)
    ).all()
    trace_id = uuid4()
    results = []
    for chunk, filename in rows:
        score = sum(chunk.text.lower().count(term.lower()) for term in terms)
        results.append(
            SearchResult(
                trace_id=trace_id,
                chunk=ChunkRead.model_validate(chunk),
                score=float(score),
                filename=filename,
            )
        )
    return sorted(results, key=lambda item: item.score, reverse=True)

