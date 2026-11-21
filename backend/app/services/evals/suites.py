from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from app.db.models import Document, KnowledgeSnapshot, KnowledgeSnapshotChunk, Repository
from app.services.evals.ragas_runner import RagEvalCase


DEFAULT_SUITE_DIR = Path(__file__).resolve().parents[4] / "docs" / "datas" / "evals"
AUTO_SMOKE_SUITE_ID = "auto-document-smoke"


@dataclass(frozen=True)
class LoadedEvalSuite:
    metadata: dict[str, Any]
    cases: list[RagEvalCase]


def _read_suite(path: Path) -> tuple[dict[str, Any], str]:
    raw = path.read_bytes()
    payload = json.loads(raw.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"评测集 {path.name} 必须是 JSON 对象")
    name = str(payload.get("name") or "").strip()
    cases = payload.get("cases")
    if not name or not isinstance(cases, list) or not cases:
        raise ValueError(f"评测集 {path.name} 缺少 name 或 cases")
    return payload, hashlib.sha256(raw).hexdigest()


def document_ids_by_filename(db: Session, repo_id: Any, snapshot: KnowledgeSnapshot | None = None) -> dict[str, list[str]]:
    model = KnowledgeSnapshotChunk if snapshot else Document
    documents = (
        db.query(model.id, model.source_id, model.meta)
        .filter(model.repo_id == repo_id)
        .order_by(model.created_at.asc())
    )
    if snapshot:
        documents = documents.filter(model.snapshot_id == snapshot.id)
    matches: dict[str, list[str]] = defaultdict(list)
    for document in documents:
        metadata = document.meta or {}
        candidates = {
            Path(str(metadata.get("filename") or "")).name.casefold(),
            Path(str(metadata.get("display_title") or "")).name.casefold(),
            Path(str(metadata.get("source_file") or "")).name.casefold(),
        }
        for filename in candidates - {""}:
            matches[filename].extend(str(value) for value in (document.id, document.source_id) if value)
    return {filename: list(dict.fromkeys(ids)) for filename, ids in matches.items()}


def _suite_applies_to_repo(payload: dict[str, Any], repo: Repository) -> bool:
    names = payload.get("repository_names")
    if not isinstance(names, list) or not names:
        return True
    accepted = {str(item).casefold() for item in names if str(item).strip()}
    return repo.name.casefold() in accepted or repo.full_name.casefold() in accepted


def _suite_metadata(
    source_ids: dict[str, list[str]],
    path: Path,
    payload: dict[str, Any],
    sha256: str,
) -> dict[str, Any]:
    missing_sources: list[str] = []
    for item in payload["cases"]:
        source_file = str(item.get("source_file") or "").strip()
        if source_file and not source_ids.get(Path(source_file).name.casefold()):
            missing_sources.append(source_file)
    missing_sources = list(dict.fromkeys(missing_sources))
    return {
        "id": str(payload["name"]),
        "name": str(payload["name"]),
        "label": str(payload.get("label") or payload["name"]),
        "description": str(payload.get("description") or "人工维护的固定回归评测集"),
        "schema_version": int(payload.get("schema_version") or 1),
        "case_count": len(payload["cases"]),
        "sha256": sha256,
        "source": "fixed",
        "filename": path.name,
        "ready": not missing_sources,
        "missing_sources": missing_sources,
        "recommended": bool(payload.get("recommended", False)),
    }


def list_eval_suites(
    db: Session,
    repo_id: Any,
    *,
    suite_dir: Path = DEFAULT_SUITE_DIR,
    snapshot: KnowledgeSnapshot | None = None,
) -> list[dict[str, Any]]:
    repo = db.get(Repository, repo_id)
    if repo is None:
        raise ValueError(f"Repository {repo_id} was not found")
    suites: list[dict[str, Any]] = []
    source_ids = None
    if suite_dir.is_dir():
        for path in sorted(suite_dir.glob("*.json")):
            payload, sha256 = _read_suite(path)
            if _suite_applies_to_repo(payload, repo):
                if source_ids is None:
                    source_ids = document_ids_by_filename(db, repo.id, snapshot)
                suites.append(_suite_metadata(source_ids, path, payload, sha256))
    suites.sort(key=lambda item: (not item["recommended"], not item["ready"], item["label"]))
    suites.append(
        {
            "id": AUTO_SMOKE_SUITE_ID,
            "name": AUTO_SMOKE_SUITE_ID,
            "label": "自动文档冒烟测试",
            "description": "从当前仓库文档临时出题，只验证链路能运行，不能作为发布基线。",
            "schema_version": 1,
            "case_count": None,
            "sha256": None,
            "source": "auto_smoke",
            "filename": None,
            "ready": True,
            "missing_sources": [],
            "recommended": False,
        }
    )
    return suites


def load_eval_suite(
    db: Session,
    repo_id: Any,
    suite_id: str,
    *,
    suite_dir: Path = DEFAULT_SUITE_DIR,
    snapshot: KnowledgeSnapshot | None = None,
) -> LoadedEvalSuite:
    if suite_id == AUTO_SMOKE_SUITE_ID:
        return LoadedEvalSuite(
            metadata={
                "id": AUTO_SMOKE_SUITE_ID,
                "name": AUTO_SMOKE_SUITE_ID,
                "label": "自动文档冒烟测试",
                "description": "从当前仓库文档临时出题，只验证链路能运行，不能作为发布基线。",
                "source": "auto_smoke",
                "sha256": None,
                "case_count": None,
            },
            cases=[],
        )

    repo = db.get(Repository, repo_id)
    if repo is None:
        raise ValueError(f"Repository {repo_id} was not found")
    for path in sorted(suite_dir.glob("*.json")) if suite_dir.is_dir() else []:
        payload, sha256 = _read_suite(path)
        if str(payload["name"]) != suite_id or not _suite_applies_to_repo(payload, repo):
            continue
        source_ids = document_ids_by_filename(db, repo.id, snapshot)
        metadata = _suite_metadata(source_ids, path, payload, sha256)
        if not metadata["ready"]:
            missing = "、".join(metadata["missing_sources"])
            raise ValueError(f"评测集 {suite_id} 缺少已索引来源：{missing}")
        cases: list[RagEvalCase] = []
        for index, item in enumerate(payload["cases"], start=1):
            case_id = str(item.get("id") or f"case-{index}").strip()
            question = str(item.get("question") or "").strip()
            reference = str(item.get("reference") or "").strip()
            if not question or not reference:
                raise ValueError(f"评测集 {suite_id} 的 {case_id} 缺少 question 或 reference")
            source_file = str(item.get("source_file") or "").strip()
            expected_source_ids = source_ids.get(Path(source_file).name.casefold(), [])
            cases.append(
                RagEvalCase(
                    id=case_id,
                    question=question,
                    reference=reference,
                    expected_source_ids=expected_source_ids,
                    source_type=str(item.get("source_type") or "knowledge_file"),
                    required_tools=[str(value) for value in item.get("required_tools") or []],
                    expected_tool_calls=[dict(value) for value in item.get("expected_tool_calls") or []],
                    kind=str(item.get("kind") or "positive"),
                    must_include=[str(value) for value in item.get("must_include") or []],
                    must_not_include=[str(value) for value in item.get("must_not_include") or []],
                    answer_regex=str(item.get("answer_regex") or "").strip() or None,
                    source_file=source_file or None,
                )
            )
        metadata["case_count"] = len(cases)
        return LoadedEvalSuite(metadata=metadata, cases=cases)
    raise ValueError(f"未找到评测集：{suite_id}")
