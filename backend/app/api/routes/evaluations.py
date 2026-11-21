from app.services.jobs import schedule_job
import asyncio
import hashlib
import json
import logging
from contextlib import ExitStack
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from app.core.config import settings
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import ActionDraft, EvalCaseCheckpoint, EvalDatasetVersion, EvalRun, EvalTokenBudget, KnowledgeSnapshot, Repository, User
from app.db.session import SessionLocal, get_db
from app.schemas.evals import EvalDatasetPublishRequest, EvalIssueDraftRequest, EvalRunRequest, EvalRunResponse, RagEvalCaseRequest
from app.services.evals.ragas_runner import (
    REQUIRED_RAGAS_METRICS,
    RagEvalCase,
    rescore_rag_result,
    run_rag_quality_eval,
)
from app.services.evals.suites import AUTO_SMOKE_SUITE_ID, document_ids_by_filename, list_eval_suites, load_eval_suite
from app.services.evals.runner import run_basic_eval
from app.services.evals.release_gate import application_digest, gate_decision
from app.services.evals.token_budget import TokenBudget, TokenBudgetExceeded, evaluation_budget
from app.services.permissions import require_permission, write_audit_log
from app.services.rag.snapshots import pin_snapshot, selected_snapshot

router = APIRouter()
logger = logging.getLogger(__name__)
DATASET_PREFIX = "dataset:"


@router.get("/config")
def get_eval_config(_user=Depends(require_permission("eval:read"))) -> dict:
    return {
        "max_cases": settings.max_eval_cases,
        "max_tokens": settings.max_eval_tokens,
        "answer_model": settings.llm_model,
        "judge_model": settings.ragas_judge_model or settings.llm_model,
    }


def _request_case(item) -> RagEvalCase:
    return RagEvalCase(
        id=item.id,
        question=item.question,
        reference=item.reference,
        expected_source_ids=item.expected_source_ids,
        source_type=item.source_type,
        required_tools=item.required_tools,
        expected_tool_calls=item.expected_tool_calls,
        kind=item.kind,
        must_include=item.must_include,
        must_not_include=item.must_not_include,
        answer_regex=item.answer_regex,
        source_file=item.source_file,
    )


def _custom_evaluation_set(payload: EvalRunRequest) -> dict:
    serialized = [item.model_dump(mode="json") for item in payload.cases or []]
    canonical = json.dumps(serialized, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {
        "id": payload.name or "custom-cases",
        "name": payload.name or "custom-cases",
        "label": payload.name or "自定义评测集",
        "description": "通过 API 提交的固定评测用例",
        "source": "api_cases",
        "sha256": hashlib.sha256(canonical).hexdigest(),
        "case_count": len(serialized),
    }


def _published_suite(
    db: Session, row: EvalDatasetVersion, source_ids: dict[str, list[str]] | None = None,
) -> tuple[dict, list[RagEvalCase]]:
    cases = []
    missing_sources = set()
    for item in row.cases:
        case = _request_case(RagEvalCaseRequest.model_validate(item))
        if case.source_file:
            if source_ids is None:
                source_ids = document_ids_by_filename(db, row.repo_id)
            documents = source_ids.get(Path(case.source_file).name.casefold(), [])
            if not documents:
                missing_sources.add(case.source_file)
            case.expected_source_ids = list(dict.fromkeys([
                *case.expected_source_ids,
                *documents,
            ]))
        cases.append(case)
    return {
        "id": f"{DATASET_PREFIX}{row.id}", "name": row.dataset_key, "label": f"{row.label} · v{row.version}",
        "description": row.description, "schema_version": 1, "version": row.version,
        "case_count": len(cases), "sha256": row.sha256, "source": "published",
        "missing_sources": sorted(missing_sources), "ready": not missing_sources, "recommended": False,
    }, cases


@router.post("/datasets/{repo_id}/publish", status_code=201)
def publish_dataset(
    repo_id: UUID,
    payload: EvalDatasetPublishRequest,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("repo:sync")),
) -> dict:
    if db.get(Repository, repo_id) is None:
        raise HTTPException(404, "未找到仓库")
    label = payload.label.strip()
    if not label:
        raise HTTPException(422, "评测集名称不能为空")
    cases = []
    seen = set()
    for index, item in enumerate(payload.cases, start=1):
        case_id = item.id.strip() or f"case-{index}"
        question, reference = item.question.strip(), item.reference.strip()
        if case_id in seen or len(case_id) > 100 or not question or not reference:
            raise HTTPException(422, f"第 {index} 题需要唯一且不超过 100 字符的 ID、问题和参考答案")
        seen.add(case_id)
        cases.append({
            **item.model_dump(mode="json"), "id": case_id, "question": question, "reference": reference,
            "source_file": (item.source_file or "").strip() or None,
        })
    version = db.scalar(select(func.max(EvalDatasetVersion.version)).where(
        EvalDatasetVersion.repo_id == repo_id, EvalDatasetVersion.dataset_key == payload.dataset_key,
    ))
    row = EvalDatasetVersion(
        repo_id=repo_id, dataset_key=payload.dataset_key, version=(version or 0) + 1, label=label,
        description=payload.description.strip(), cases=cases,
        sha256=hashlib.sha256(json.dumps(cases, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
    )
    try:
        db.add(row)
        db.flush()
        write_audit_log(
            db, user=user, repo_id=repo_id, action="eval_dataset.publish",
            target_type="eval_dataset_version", target_id=str(row.id), result_json={"sha256": row.sha256},
        )
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(409, "评测集版本已被其他请求发布，请刷新后重试") from exc
    return _published_suite(db, row)[0]


@router.get("/datasets/{repo_id}/{dataset_id}")
def dataset_version(
    repo_id: UUID,
    dataset_id: UUID,
    db: Session = Depends(get_db),
    _user=Depends(require_permission("eval:read")),
) -> dict:
    row = db.get(EvalDatasetVersion, dataset_id)
    if row is None or row.repo_id != repo_id:
        raise HTTPException(404, "未找到评测集版本")
    return {"suite": _published_suite(db, row)[0], "cases": row.cases}


@router.get("/suites")
async def get_eval_suites(
    repo_id: UUID = Query(...),
    snapshot_id: UUID | None = None,
    db: Session = Depends(get_db),
    _user=Depends(require_permission("eval:read")),
) -> list[dict]:
    snapshot = db.get(KnowledgeSnapshot, snapshot_id) if snapshot_id else None
    if snapshot_id and (snapshot is None or snapshot.repo_id != repo_id):
        raise HTTPException(404, "未找到知识快照")
    if snapshot is None:
        snapshot = selected_snapshot(db, repo_id)
    try:
        suites = list_eval_suites(db, repo_id, snapshot=snapshot)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    versions = db.scalars(select(EvalDatasetVersion).where(EvalDatasetVersion.repo_id == repo_id)
                          .order_by(EvalDatasetVersion.created_at.desc(), EvalDatasetVersion.version.desc())).all()
    source_ids = document_ids_by_filename(db, repo_id, snapshot) if any(
        item.get("source_file") for row in versions for item in row.cases
    ) else {}
    return [_published_suite(db, row, source_ids)[0] for row in versions] + suites


@router.post("/run", response_model=EvalRunResponse)
async def run_eval(
    payload: EvalRunRequest,
    db: Session = Depends(get_db),
    _user=Depends(require_permission("eval:read")),
) -> EvalRunResponse:
    result = await run_basic_eval(db, str(payload.repo_id) if payload.repo_id else None)
    eval_run = EvalRun(name=payload.name, result_json=result)
    db.add(eval_run)
    db.commit()
    db.refresh(eval_run)
    return EvalRunResponse(
        eval_id=str(eval_run.id),
        status="completed",
        result=result,
    )


@router.post("/rag/run", response_model=EvalRunResponse)
async def run_rag_eval(
    payload: EvalRunRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    _user=Depends(require_permission("eval:read")),
) -> EvalRunResponse:
    if payload.repo_id is None:
        raise HTTPException(status_code=400, detail="RAG Eval 需要 repo_id")
    if db.get(Repository, payload.repo_id) is None:
        raise HTTPException(status_code=404, detail="未找到仓库")
    if payload.cases is not None and payload.suite_id:
        raise HTTPException(status_code=400, detail="cases 与 suite_id 不能同时提交")
    if payload.snapshot_id:
        snapshot = db.get(KnowledgeSnapshot, payload.snapshot_id)
        if snapshot is None or snapshot.repo_id != payload.repo_id:
            raise HTTPException(404, "未找到知识快照")
    else:
        snapshot = selected_snapshot(db, payload.repo_id)
    if payload.cases is not None:
        cases = [_request_case(item) for item in payload.cases]
        evaluation_set = _custom_evaluation_set(payload)
    elif payload.suite_id and payload.suite_id.startswith(DATASET_PREFIX):
        try:
            dataset_id = UUID(payload.suite_id.removeprefix(DATASET_PREFIX))
        except ValueError as exc:
            raise HTTPException(400, "评测集版本 ID 无效") from exc
        row = db.get(EvalDatasetVersion, dataset_id)
        if row is None or row.repo_id != payload.repo_id:
            raise HTTPException(404, "未找到评测集版本")
        source_ids = document_ids_by_filename(db, payload.repo_id, snapshot) if snapshot else None
        evaluation_set, cases = _published_suite(db, row, source_ids)
        if not evaluation_set["ready"]:
            raise HTTPException(422, "评测集缺少已索引来源：" + "、".join(evaluation_set["missing_sources"]))
        if payload.case_budget is None:
            raise HTTPException(422, "请为版本化评测设置用例预算")
    elif payload.suite_id:
        try:
            suite = load_eval_suite(db, payload.repo_id, payload.suite_id, snapshot=snapshot)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        cases = None if payload.suite_id == AUTO_SMOKE_SUITE_ID else suite.cases
        evaluation_set = suite.metadata
    else:
        cases = None
        evaluation_set = {
            "id": AUTO_SMOKE_SUITE_ID,
            "name": AUTO_SMOKE_SUITE_ID,
            "label": "自动文档冒烟测试",
            "description": "未指定固定评测集，使用当前仓库文档临时出题。",
            "source": "auto_smoke",
            "sha256": None,
            "case_count": None,
        }
    if cases is not None and len(cases) > settings.max_eval_cases:
        raise HTTPException(status_code=422, detail="评测用例数量超出单任务限制")
    if payload.token_budget is not None and payload.token_budget > settings.max_eval_tokens:
        raise HTTPException(422, f"Token 预算不能超过 {settings.max_eval_tokens}")
    if payload.token_budget is not None and db.get_bind().dialect.name != "postgresql" and any((
        settings.llm_api_key, settings.ragas_judge_api_key, settings.dashscope_api_key, settings.embedding_api_key,
        settings.rerank_api_key,
    )):
        raise HTTPException(422, "真实模型的 Token 预算评测需要 PostgreSQL")
    if snapshot:
        evaluation_set["snapshot"] = {"id": str(snapshot.id), "sha256": snapshot.sha256, "label": snapshot.label}
    evaluation_set["code_sha256"] = application_digest()
    if payload.case_budget is not None or payload.token_budget is not None:
        if not cases or any(not item.id.strip() or len(item.id) > 100 for item in cases) or len({item.id for item in cases}) != len(cases):
            raise HTTPException(422, "预算评测需要非空固定题集和唯一且不超过 100 字符的用例 ID")
        if payload.case_budget is not None and len(cases) > payload.case_budget:
            raise HTTPException(422, f"评测集有 {len(cases)} 题，超出 {payload.case_budget} 题预算")
        evaluation_set["case_budget"] = payload.case_budget or len(cases)
        evaluation_set["token_budget"] = payload.token_budget
        evaluation_set["execution_config"] = {
            "answer_model": settings.llm_model,
            "judge_model": (settings.ragas_judge_model or settings.llm_model) if payload.run_judge else None,
            "top_k": payload.top_k,
        }
    result = _job_result(
        status="queued",
        stage="queued",
        message="评测任务已提交，正在等待后台执行。",
        repo_id=str(payload.repo_id),
        evaluation_set=evaluation_set,
    )
    eval_run = EvalRun(name=payload.name or "rag_eval", result_json=result)
    db.add(eval_run)
    db.flush()
    if payload.token_budget is not None:
        db.add(EvalTokenBudget(eval_id=eval_run.id, token_limit=payload.token_budget))
    db.refresh(eval_run)
    schedule_job(db, background_tasks,
        _execute_rag_eval_job,
        str(eval_run.id),
        str(payload.repo_id),
        cases,
        payload.top_k,
        payload.run_judge,
        evaluation_set,
    )
    return EvalRunResponse(eval_id=str(eval_run.id), status="queued", result=result)


async def _execute_rag_eval_job(
    eval_id: str,
    repo_id: str,
    cases: list[RagEvalCase] | None,
    limit: int,
    run_judge: bool,
    evaluation_set: dict,
) -> None:
    db = SessionLocal()
    # Yield once after Starlette sends the queued response so the client receives
    # the Eval ID before imports, index loading, or model setup can block the loop.
    await asyncio.sleep(0)
    tracked = evaluation_set.get("case_budget") is not None
    eval_uuid = UUID(eval_id)
    completed_rows: dict[str, dict] = {}
    budget = TokenBudget(eval_uuid) if evaluation_set.get("token_budget") is not None else None

    def save_case(row: dict) -> None:
        checkpoint = db.get(EvalCaseCheckpoint, (eval_uuid, row["id"]))
        checkpoint.result = row
        db.commit()
        completed_rows[row["id"]] = row

    async def persist_progress(progress: dict) -> None:
        if budget and budget.summary()["blocked"]:
            raise TokenBudgetExceeded("Token 预算不足，已停止评测")
        if tracked and progress.get("stage") == "chatagent":
            case_id = progress["case_id"]
            if db.get(EvalCaseCheckpoint, (eval_uuid, case_id)) is not None:
                raise RuntimeError(f"第 {case_id} 题上一次执行中断，已保留其预算记录")
            db.add(EvalCaseCheckpoint(eval_id=eval_uuid, case_id=case_id))
        eval_run = db.get(EvalRun, UUID(eval_id))
        if eval_run is None:
            return
        eval_run.result_json = _job_result(
            status="running",
            stage=str(progress.get("stage") or "running"),
            message=str(progress.get("message") or "评测正在后台运行。"),
            repo_id=repo_id,
            evaluation_set=evaluation_set,
            **{
                key: value
                for key, value in progress.items()
                if key not in {"stage", "message"}
            },
        )
        db.add(eval_run)
        db.commit()

    try:
        if evaluation_set.get("code_sha256") and evaluation_set["code_sha256"] != application_digest():
            raise RuntimeError("运行代码版本已经变化，请重新提交评测")
        if tracked:
            previous = db.get(EvalRun, eval_uuid)
            if previous is None or previous.result_json.get("status") not in {"queued", "running"}:
                db.close()
                return
            checkpoints = db.scalars(select(EvalCaseCheckpoint).where(EvalCaseCheckpoint.eval_id == eval_uuid))
            completed_rows = {row.case_id: row.result for row in checkpoints if row.result is not None}
            expected = evaluation_set.get("execution_config")
            if expected is not None and expected != {
                "answer_model": settings.llm_model,
                "judge_model": (settings.ragas_judge_model or settings.llm_model) if run_judge else None,
                "top_k": limit,
            }:
                raise RuntimeError("模型或检索配置已变化，请重新提交评测")
        await persist_progress({"stage": "starting", "message": "后台任务已启动，正在加载仓库上下文。"})
        with ExitStack() as stack:
            stack.enter_context(evaluation_budget(budget))
            if evaluation_set.get("snapshot"):
                stack.enter_context(pin_snapshot(UUID(repo_id), UUID(evaluation_set["snapshot"]["id"])))
            result = await run_rag_quality_eval(
                db, repo_id, cases=cases, limit=limit, run_judge=run_judge,
                progress_callback=persist_progress, evaluation_set=evaluation_set,
                resume_rows=completed_rows if tracked else None, case_callback=save_case if tracked else None,
            )
        result["progress"] = {
            "stage": "completed",
            "message": "评测已完成。",
            "updated_at": datetime.now(UTC).isoformat(),
        }
    except Exception as exc:
        db.rollback()
        blocked = budget is not None and budget.summary()["blocked"]
        failure = "Token 预算不足，已拦截模型请求并保留已完成用例" if blocked else str(exc)
        if blocked:
            logger.info("Evaluation stopped at its token budget: eval_id=%s", eval_id)
        else:
            logger.exception("RAGAS background evaluation failed: eval_id=%s", eval_id)
        result = _job_result(
            status="error",
            stage="budget_exhausted" if blocked else "error",
            message=failure,
            repo_id=repo_id,
            evaluation_set=evaluation_set,
        )
        result.update(
            {
                "passed": False,
                "ragas": {
                    "requested": run_judge,
                    "available": False,
                    "passed": None,
                    "error": failure,
                    "metrics": {},
                },
                "cases": list(completed_rows.values()),
            }
        )

    try:
        if budget:
            result["token_budget"] = budget.summary()
        eval_run = db.get(EvalRun, UUID(eval_id))
        if eval_run is not None:
            eval_run.result_json = result
            db.add(eval_run)
            db.commit()
    finally:
        db.close()


def _job_result(
    *,
    status: str,
    stage: str,
    message: str,
    repo_id: str | None = None,
    evaluation_set: dict | None = None,
    **progress: object,
) -> dict:
    return {
        "kind": "chatagent_ragas",
        "status": status,
        "execution_status": status,
        "quality_status": "incomplete",
        "evaluation_complete": False,
        "passed": False,
        "target": {"name": "ChatAgent", "repo_id": repo_id} if repo_id else {"name": "ChatAgent"},
        "evaluation_set": evaluation_set or {},
        "progress": {
            "stage": stage,
            "message": message,
            "updated_at": datetime.now(UTC).isoformat(),
            **progress,
        },
    }


@router.post("/{eval_id}/rescore", response_model=EvalRunResponse)
async def rescore_eval(
    eval_id: UUID,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    _user=Depends(require_permission("eval:read")),
) -> EvalRunResponse:
    source = db.get(EvalRun, eval_id)
    if source is None:
        raise HTTPException(status_code=404, detail="未找到 Eval run")
    source_result = source.result_json or {}
    if len(source_result.get("cases") or []) > settings.max_eval_cases:
        raise HTTPException(status_code=422, detail="重评分用例数量超出单任务限制")
    if not source_result.get("cases"):
        raise HTTPException(status_code=400, detail="原评测没有可用于重评分的逐题现场")
    error_count = sum(bool(item.get("ragas_errors")) for item in source_result.get("cases") or [])
    missing_count = sum(
        any(name not in (item.get("ragas") or {}) for name in ("context_precision", "context_recall", "faithfulness", "answer_relevancy", "agent_goal_accuracy"))
        for item in source_result.get("cases") or []
    )
    if error_count == 0 and missing_count == 0:
        raise HTTPException(status_code=400, detail="原评测没有失败或缺失的 Judge 指标")
    repo_id = str((source_result.get("target") or {}).get("repo_id") or "")
    evaluation_set = dict(source_result.get("evaluation_set") or {})
    queued = _job_result(
        status="queued",
        stage="queued",
        message=f"已提交重评分任务，将复用 Eval {str(eval_id)[:8]} 的 ChatAgent 现场。",
        repo_id=repo_id or None,
        evaluation_set=evaluation_set,
        rescore_of=str(eval_id),
    )
    target = EvalRun(name=f"rescore-{source.name}", result_json=queued)
    db.add(target)
    db.flush()
    if evaluation_set.get("token_budget") is not None:
        evaluation_set["token_budget"] = min(evaluation_set["token_budget"], settings.max_eval_tokens)
        db.add(EvalTokenBudget(eval_id=target.id, token_limit=evaluation_set["token_budget"]))
    db.refresh(target)
    schedule_job(db, background_tasks,
        _execute_rescore_job,
        str(target.id),
        str(source.id),
        source_result,
        repo_id,
        evaluation_set,
    )
    return EvalRunResponse(eval_id=str(target.id), status="queued", result=queued)


async def _execute_rescore_job(
    target_eval_id: str,
    source_eval_id: str,
    source_result: dict,
    repo_id: str,
    evaluation_set: dict,
) -> None:
    db = SessionLocal()
    await asyncio.sleep(0)

    budget = TokenBudget(UUID(target_eval_id)) if evaluation_set.get("token_budget") is not None else None

    async def persist_progress(progress: dict) -> None:
        if budget and budget.summary()["blocked"]:
            raise TokenBudgetExceeded("Token 预算不足，已停止重评分")
        row = db.get(EvalRun, UUID(target_eval_id))
        if row is None:
            return
        row.result_json = _job_result(
            status="running",
            stage=str(progress.get("stage") or "rescore"),
            message=str(progress.get("message") or "正在重试异常评分。"),
            repo_id=repo_id or None,
            evaluation_set=evaluation_set,
            rescore_of=source_eval_id,
            **{key: value for key, value in progress.items() if key not in {"stage", "message"}},
        )
        db.add(row)
        db.commit()

    try:
        with evaluation_budget(budget):
            result = await rescore_rag_result(
                source_result, eval_id=source_eval_id, progress_callback=persist_progress,
            )
        result["evaluation_set"] = evaluation_set
        result["progress"] = {
            "stage": "completed",
            "message": "异常指标重评分已完成；ChatAgent 回答和检索现场均未重新运行。",
            "updated_at": datetime.now(UTC).isoformat(),
        }
    except Exception as exc:
        db.rollback()
        blocked = budget is not None and budget.summary()["blocked"]
        failure = "Token 预算不足，已停止重评分" if blocked else str(exc)
        if blocked:
            logger.info("Rescore stopped at its token budget: eval_id=%s", target_eval_id)
        else:
            logger.exception("RAGAS rescore failed: eval_id=%s", target_eval_id)
        result = _job_result(
            status="error",
            stage="budget_exhausted" if blocked else "error",
            message=failure,
            repo_id=repo_id or None,
            evaluation_set=evaluation_set,
            rescore_of=source_eval_id,
        )
        result["ragas"] = {
            "requested": True,
            "available": False,
            "complete": False,
            "passed": None,
            "error": failure,
            "metrics": {},
        }
        result["cases"] = source_result.get("cases") or []
    try:
        if budget:
            result["token_budget"] = budget.summary()
        row = db.get(EvalRun, UUID(target_eval_id))
        if row is not None:
            row.result_json = result
            db.add(row)
            db.commit()
    finally:
        db.close()


def _eval_comparison(base: EvalRun, candidate: EvalRun) -> dict:
    base_result = base.result_json or {}
    candidate_result = candidate.result_json or {}
    base_set = base_result.get("evaluation_set") or {}
    candidate_set = candidate_result.get("evaluation_set") or {}
    reasons: list[str] = []
    if (base_result.get("target") or {}).get("repo_id") != (candidate_result.get("target") or {}).get("repo_id"):
        reasons.append("评测仓库不一致")
    if base_set.get("sha256") != candidate_set.get("sha256"):
        reasons.append("评测集 Hash 不一致")
    if (base_result.get("ragas") or {}).get("version") != (candidate_result.get("ragas") or {}).get("version"):
        reasons.append("Ragas 版本不一致")
    if (base_result.get("ragas") or {}).get("judge_model") != (candidate_result.get("ragas") or {}).get("judge_model"):
        reasons.append("Judge 模型不一致")
    if (base_result.get("pipeline") or {}).get("top_k") != (candidate_result.get("pipeline") or {}).get("top_k"):
        reasons.append("Top-K 不一致")
    base_metrics = base_result.get("summary_metrics") or {}
    candidate_metrics = candidate_result.get("summary_metrics") or {}
    metric_deltas = []
    for name in sorted(set(base_metrics) | set(candidate_metrics)):
        left = base_metrics.get(name)
        right = candidate_metrics.get(name)
        if not isinstance(left, (int, float)) or not isinstance(right, (int, float)):
            continue
        metric_deltas.append(
            {"name": name, "base": float(left), "candidate": float(right), "delta": round(float(right) - float(left), 4)}
        )
    base_cases = {str(item.get("id")): item for item in base_result.get("cases") or []}
    candidate_cases = {str(item.get("id")): item for item in candidate_result.get("cases") or []}
    case_deltas = []
    for case_id in sorted(set(base_cases) | set(candidate_cases)):
        left = base_cases.get(case_id) or {}
        right = candidate_cases.get(case_id) or {}
        left_passed = bool((left.get("hard_rules") or {}).get("passed")) and not bool(left.get("ragas_errors"))
        right_passed = bool((right.get("hard_rules") or {}).get("passed")) and not bool(right.get("ragas_errors"))
        state = "unchanged"
        if not left_passed and right_passed:
            state = "improved"
        elif left_passed and not right_passed:
            state = "regressed"
        case_deltas.append(
            {"id": case_id, "base_passed": left_passed, "candidate_passed": right_passed, "state": state}
        )
    return {
        "base_eval_id": str(base.id),
        "candidate_eval_id": str(candidate.id),
        "comparable": not reasons,
        "incompatibility_reasons": reasons,
        "metric_deltas": metric_deltas,
        "case_deltas": case_deltas,
        "improved_cases": sum(item["state"] == "improved" for item in case_deltas),
        "regressed_cases": sum(item["state"] == "regressed" for item in case_deltas),
        "candidate_quality_status": candidate_result.get("quality_status"),
        "candidate_complete": bool(candidate_result.get("evaluation_complete")),
        "release_gate_passed": bool(not reasons and candidate_result.get("passed")),
    }


@router.get("/compare")
async def compare_evals(
    base_eval_id: UUID = Query(...),
    candidate_eval_id: UUID = Query(...),
    db: Session = Depends(get_db),
    _user=Depends(require_permission("eval:read")),
) -> dict:
    base = db.get(EvalRun, base_eval_id)
    candidate = db.get(EvalRun, candidate_eval_id)
    if base is None or candidate is None:
        raise HTTPException(status_code=404, detail="未找到用于对比的 Eval run")
    return _eval_comparison(base, candidate)


def _issue_markdown(eval_run: EvalRun) -> str:
    result = eval_run.result_json or {}
    suite = result.get("evaluation_set") or {}
    ragas = result.get("ragas") or {}
    lines = [
        "## 复现环境",
        "",
        f"- Eval ID：`{eval_run.id}`",
        f"- 评测集：`{suite.get('name') or '-'}`",
        f"- 评测集 Hash：`{suite.get('sha256') or '-'}`",
        f"- Ragas：`{ragas.get('version') or '-'}`",
        f"- Judge：`{ragas.get('judge_model') or '-'}`",
        f"- 运行状态：`{result.get('status') or '-'}`",
        f"- 质量状态：`{result.get('quality_status') or '-'}`",
        "",
        "## 聚合结果",
        "",
        "| 指标 | 分数 | 门槛 |",
        "| --- | ---: | ---: |",
    ]
    for name, detail in (ragas.get("metrics") or {}).items():
        if name not in REQUIRED_RAGAS_METRICS:
            continue
        score = detail.get("score")
        threshold = detail.get("threshold")
        lines.append(
            f"| {name} | {score if score is not None else 'ERROR'} | {threshold if threshold is not None else '-'} |"
        )
    lines.extend(["", "## 失败用例", ""])
    for case in result.get("cases") or []:
        diagnoses = [item for item in case.get("diagnoses") or [] if item.get("category") != "passed"]
        if not diagnoses and not case.get("ragas_errors"):
            continue
        lines.extend(
            [
                f"### {case.get('id') or 'unknown'}",
                "",
                f"- 问题：{case.get('question') or '-'}",
                f"- 标准答案：{case.get('reference') or '-'}",
                f"- 实际答案：{case.get('response') or '-'}",
            ]
        )
        if case.get("ragas_errors"):
            lines.append(f"- Judge 错误：`{json.dumps(case['ragas_errors'], ensure_ascii=False)}`")
        for diagnosis in diagnoses:
            lines.append(f"- {diagnosis.get('title')}：{diagnosis.get('action')}")
        lines.append("")
    lines.extend(
        [
            "## 验收标准",
            "",
            "- 使用同一评测集版本重新运行。",
            "- Judge 指标计算零错误。",
            "- 所有硬规则通过，所有门禁指标达到当前阈值。",
            "- 与本 Eval 做逐题对比，不新增回归用例。",
        ]
    )
    return "\n".join(lines)


@router.post("/{eval_id}/issue-draft")
async def create_eval_issue_draft(
    eval_id: UUID,
    payload: EvalIssueDraftRequest,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("draft:create")),
) -> dict:
    eval_run = db.get(EvalRun, eval_id)
    if eval_run is None:
        raise HTTPException(status_code=404, detail="未找到 Eval run")
    result = eval_run.result_json or {}
    if budget := db.get(EvalTokenBudget, eval_uuid):
        result = {**result, "token_budget": {
            "limit": budget.token_limit, "committed": budget.committed_tokens,
            "reported": budget.reported_tokens, "blocked": budget.blocked,
        }}
    repo_id = (result.get("target") or {}).get("repo_id")
    if not repo_id:
        raise HTTPException(status_code=400, detail="该历史评测没有保存仓库信息，无法创建 Issue 草稿")
    repo = db.get(Repository, UUID(str(repo_id)))
    if repo is None:
        raise HTTPException(status_code=404, detail="未找到评测对应仓库")
    suite_label = (result.get("evaluation_set") or {}).get("label") or "RAGAS"
    title = payload.title or f"RAG 质量回归：{suite_label} 未通过"
    draft = ActionDraft(
        repo_id=repo.id,
        draft_type="create_issue",
        target_type="repository",
        title=title,
        content=_issue_markdown(eval_run),
        risk_level="medium",
        created_by=user.id,
        meta={"eval_id": str(eval_run.id), "quality_status": result.get("quality_status")},
    )
    db.add(draft)
    write_audit_log(
        db,
        user=user,
        repo_id=repo.id,
        action="eval.issue_draft.create",
        target_type="eval_run",
        target_id=str(eval_run.id),
        request_json={"eval_id": str(eval_run.id), "title": title},
    )
    db.commit()
    db.refresh(draft)
    return {"draft_id": str(draft.id), "status": draft.status, "title": draft.title}


@router.get("")
async def list_evals(
    limit: int = 20,
    repo_id: UUID | None = None,
    db: Session = Depends(get_db),
    _user=Depends(require_permission("eval:read")),
) -> list[dict]:
    runs = select(EvalRun)
    if repo_id is not None:
        runs = runs.where(EvalRun.result_json["target"]["repo_id"].as_string() == str(repo_id))
    rows = db.scalars(runs.order_by(EvalRun.created_at.desc(), EvalRun.id).limit(max(1, min(limit, 100))))
    return [
        {
            "eval_id": str(row.id),
            "name": row.name,
            "status": str((row.result_json or {}).get("status") or "completed"),
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "metrics": _summary_metrics(row.result_json or {}),
            "result": row.result_json,
        }
        for row in rows
    ]


@router.get("/{eval_id}")
async def get_eval(
    eval_id: str,
    db: Session = Depends(get_db),
    _user=Depends(require_permission("eval:read")),
) -> dict:
    try:
        eval_uuid = UUID(eval_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Eval id 无效") from exc
    eval_run = db.get(EvalRun, eval_uuid)
    if eval_run is None:
        raise HTTPException(status_code=404, detail="未找到 Eval run")
    result = eval_run.result_json or {}
    return {
        "eval_id": str(eval_run.id),
        "name": eval_run.name,
        "status": str(result.get("status") or "completed"),
        "result": result,
    }


@router.get("/{eval_id}/release-gate")
def get_release_gate(
    eval_id: UUID, snapshot_id: UUID | None = None, db: Session = Depends(get_db),
    _user=Depends(require_permission("eval:read")),
) -> dict:
    run = db.get(EvalRun, eval_id)
    if run is None:
        raise HTTPException(404, "未找到评测")
    snapshot = db.get(KnowledgeSnapshot, snapshot_id) if snapshot_id else None
    if snapshot_id and snapshot is None:
        raise HTTPException(404, "未找到知识快照")
    return gate_decision(db, run, snapshot=snapshot)


def _summary_metrics(result: dict) -> dict[str, float]:
    summary = result.get("summary_metrics")
    metrics = {
        key: float(value)
        for key, value in (summary.items() if isinstance(summary, dict) else result.items())
        if isinstance(value, (int, float)) and not isinstance(value, bool)
    }
    return metrics
