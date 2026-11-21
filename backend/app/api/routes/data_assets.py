from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models import ActiveKnowledgeSnapshot, EvalRun, Issue, KnowledgeSnapshot, KnowledgeSourceDocument, ProjectIndexState, PullRequest, Repository, User, WorkflowRun
from app.db.session import get_db
from app.services.evals.release_gate import gate_decision
from app.services.permissions import require_permission, write_audit_log
from app.services.rag.snapshots import create_snapshot, lock_repository, snapshot_summary

router = APIRouter()

ASSETS = {
    "documents": (KnowledgeSourceDocument, KnowledgeSourceDocument.name),
    "issues": (Issue, Issue.title),
    "pull_requests": (PullRequest, PullRequest.title),
    "workflow_runs": (WorkflowRun, WorkflowRun.name),
}


class SnapshotCreate(BaseModel):
    label: str = Field(min_length=1, max_length=160)


class SnapshotSelection(BaseModel):
    snapshot_id: UUID | None = None
    eval_id: UUID | None = None


@router.get("/{repo_id}/snapshots")
def list_snapshots(repo_id: UUID, db: Session = Depends(get_db)) -> dict:
    active = db.get(ActiveKnowledgeSnapshot, repo_id)
    rows = db.scalars(select(KnowledgeSnapshot).where(KnowledgeSnapshot.repo_id == repo_id)
                     .order_by(KnowledgeSnapshot.created_at.desc(), KnowledgeSnapshot.id))
    return {"active_id": active.snapshot_id if active else None, "items": [snapshot_summary(row) for row in rows]}


@router.post("/{repo_id}/snapshots", status_code=201)
def save_snapshot(
    repo_id: UUID, payload: SnapshotCreate, db: Session = Depends(get_db),
    user: User = Depends(require_permission("repo:sync")),
) -> dict:
    label = payload.label.strip()
    if not label:
        raise HTTPException(422, "快照名称不能为空")
    row = create_snapshot(db, repo_id, label)
    write_audit_log(db, user=user, repo_id=repo_id, action="knowledge_snapshot.create",
                    target_type="knowledge_snapshot", target_id=str(row.id), result_json={"sha256": row.sha256})
    db.commit()
    return snapshot_summary(row)


@router.post("/{repo_id}/snapshots/select")
def select_snapshot(
    repo_id: UUID, payload: SnapshotSelection, db: Session = Depends(get_db),
    user: User = Depends(require_permission("repo:sync")),
) -> dict:
    lock_repository(db, repo_id)
    active = db.get(ActiveKnowledgeSnapshot, repo_id)
    old_id = str(active.snapshot_id) if active else None
    gate_enabled = settings.release_gate_required or bool(settings.release_gate_eval_id)
    if gate_enabled and (not payload.snapshot_id or not payload.eval_id):
        raise HTTPException(409, "需要评测通过的知识快照和评测 ID")
    if payload.snapshot_id:
        row = db.get(KnowledgeSnapshot, payload.snapshot_id)
        if row is None or row.repo_id != repo_id:
            raise HTTPException(404, "未找到知识快照")
        if payload.eval_id:
            run = db.get(EvalRun, payload.eval_id)
            if run is None:
                raise HTTPException(404, "未找到评测")
            decision = gate_decision(db, run, snapshot=row)
            if not decision["allowed"]:
                raise HTTPException(409, "发布门禁未通过：" + "、".join(decision["reasons"]))
        if active:
            active.snapshot_id = row.id
        else:
            db.add(ActiveKnowledgeSnapshot(repo_id=repo_id, snapshot_id=row.id))
    elif active:
        db.delete(active)
    new_id = str(payload.snapshot_id) if payload.snapshot_id else None
    write_audit_log(db, user=user, repo_id=repo_id, action="knowledge_snapshot.select",
                    target_type="knowledge_snapshot", target_id=new_id,
                    result_json={"previous_id": old_id, "eval_id": str(payload.eval_id) if payload.eval_id else None})
    db.commit()
    return {"active_id": new_id}


@router.get("/{repo_id}")
def data_catalog(
    repo_id: UUID,
    kind: Literal["documents", "issues", "pull_requests", "workflow_runs"] = "documents",
    query: str = Query(default="", max_length=200),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=25, ge=1, le=100),
    db: Session = Depends(get_db),
) -> dict:
    repo = db.get(Repository, repo_id)
    if repo is None:
        raise HTTPException(404, "未找到仓库")

    counts = {
        name: db.scalar(select(func.count()).select_from(model).where(model.repo_id == repo_id))
        for name, (model, _) in ASSETS.items()
    }
    document_status = dict(db.execute(
        select(KnowledgeSourceDocument.status, func.count())
        .where(KnowledgeSourceDocument.repo_id == repo_id)
        .group_by(KnowledgeSourceDocument.status)
    ).all())
    index = db.scalar(select(ProjectIndexState).where(ProjectIndexState.repo_id == repo_id))

    model, title = ASSETS[kind]
    filters = [model.repo_id == repo_id]
    if query.strip():
        filters.append(func.lower(title).contains(query.strip().lower(), autoescape=True))
    rows = db.scalars(
        select(model).where(*filters)
        .order_by(model.updated_at.desc().nulls_last(), model.id)
        .offset(offset).limit(limit)
    )
    return {
        "counts": counts,
        "document_status": document_status,
        "matched": db.scalar(select(func.count()).select_from(model).where(*filters)),
        "source": {"name": repo.full_name, "provider": repo.provider, "status": repo.sync_status, "last_sync_at": repo.last_sync_at},
        "project_index": None if index is None else {
            "status": index.status, "last_scan_at": index.last_scan_at,
            "docs_indexed": index.docs_indexed, "docs_total": index.docs_total,
        },
        "items": [_asset(row, kind) for row in rows],
    }


def _asset(row, kind: str) -> dict:
    result = {"id": row.id, "updated_at": row.updated_at}
    if kind == "documents":
        return {
            **result, "title": row.name, "status": row.status, "source": row.source_type,
            "checksum": row.checksum, "file_size": row.file_size, "chunk_count": row.chunk_count,
        }
    if kind == "workflow_runs":
        return {**result, "title": row.name, "status": row.conclusion or row.status, "commit": row.head_sha}
    return {**result, "title": row.title, "status": row.state, "number": row.number}
