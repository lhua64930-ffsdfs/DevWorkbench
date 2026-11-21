"""Immutable retrieval data and the repository's selected version."""

import hashlib
import json
from contextlib import contextmanager
from contextvars import ContextVar
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session, defer

from app.core.config import settings
from app.db.models import ActiveKnowledgeSnapshot, Document, KnowledgeSnapshot, KnowledgeSnapshotChunk, Repository
from app.services.rag.embeddings import embedding_contract
from app.services.rag.source_policy import RAG_SOURCE_TYPES, should_vectorize
from app.services.rag.vector_store import read_snapshot_vectors

_selected: ContextVar[tuple[UUID, UUID] | None] = ContextVar("knowledge_snapshot", default=None)


@contextmanager
def pin_snapshot(repo_id: UUID, snapshot_id: UUID):
    token = _selected.set((repo_id, snapshot_id))
    try:
        yield
    finally:
        _selected.reset(token)


def selected_snapshot(db: Session, repo_id: UUID | str) -> KnowledgeSnapshot | None:
    repo_id = UUID(str(repo_id))
    pinned = _selected.get()
    if pinned and pinned[0] == repo_id:
        snapshot = db.get(KnowledgeSnapshot, pinned[1])
        if snapshot is None or snapshot.repo_id != repo_id:
            raise RuntimeError("评测绑定的知识快照不可用")
        return snapshot
    active = db.get(ActiveKnowledgeSnapshot, repo_id)
    if active is None:
        return None
    snapshot = db.get(KnowledgeSnapshot, active.snapshot_id)
    if snapshot is None or snapshot.repo_id != repo_id:
        raise RuntimeError("当前知识快照不可用")
    return snapshot


def retrieval_documents(db: Session, repo_id: UUID | str, snapshot: KnowledgeSnapshot | None):
    if snapshot is None:
        return db.query(Document).filter(Document.repo_id == repo_id)
    return db.query(KnowledgeSnapshotChunk).options(defer(KnowledgeSnapshotChunk.embedding)).filter(
        KnowledgeSnapshotChunk.repo_id == repo_id, KnowledgeSnapshotChunk.snapshot_id == snapshot.id,
    )


def snapshot_summary(row: KnowledgeSnapshot) -> dict:
    return {
        "id": row.id, "label": row.label, "sha256": row.sha256, "created_at": row.created_at,
        "chunk_count": row.chunk_count, "vector_count": row.vector_count,
    }


def lock_repository(db: Session, repo_id: UUID) -> None:
    if db.scalar(select(Repository.id).where(Repository.id == repo_id).with_for_update()) is None:
        raise HTTPException(404, "未找到仓库")


def create_snapshot(db: Session, repo_id: UUID, label: str) -> KnowledgeSnapshot:
    from app.services.rag.knowledge_base import config_dict, get_or_create_config

    lock_repository(db, repo_id)
    count = db.scalar(select(func.count()).select_from(KnowledgeSnapshot).where(KnowledgeSnapshot.repo_id == repo_id))
    if count >= settings.max_knowledge_snapshots:
        raise HTTPException(409, "仓库快照数量已达上限")
    docs = db.scalars(select(Document).where(
        Document.repo_id == repo_id, Document.source_type.in_(RAG_SOURCE_TYPES),
    ).order_by(Document.id).limit(settings.max_snapshot_chunks + 1)).all()
    if not docs or len(docs) > settings.max_snapshot_chunks:
        raise HTTPException(422, "知识分块为空或超出快照容量")
    config = config_dict(get_or_create_config(db, repo_id, commit=False))
    vectors = {}
    if settings.milvus_enabled:
        contract = embedding_contract()
        eligible = [doc for doc in docs if should_vectorize(doc.source_type)]
        for doc in eligible:
            if any((doc.meta or {}).get("embedding_" + key) != contract[key] for key in ("provider", "model", "dimensions")):
                raise HTTPException(409, "分块向量配置不一致，请先重建索引")
        vectors = read_snapshot_vectors(repo_id, [doc.id for doc in eligible])

    def canonical(value) -> bytes:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()

    digest = hashlib.sha256(canonical(config))
    row = KnowledgeSnapshot(repo_id=repo_id, label=label, config=config, chunk_count=len(docs),
                            vector_count=len(vectors), sha256="")
    db.add(row)
    db.flush()
    for doc in docs:
        chunk = {"id": doc.id, "source_id": doc.source_id, "source_type": doc.source_type, "title": doc.title,
                 "content": doc.content, "meta": doc.meta or {}, "embedding": vectors.get(doc.id)}
        digest.update(canonical({**chunk, "id": str(doc.id), "source_id": str(doc.source_id)}))
        db.add(KnowledgeSnapshotChunk(snapshot_id=row.id, repo_id=repo_id, created_at=doc.created_at, **chunk))
    row.sha256 = digest.hexdigest()
    db.flush()
    return row
