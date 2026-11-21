"""Bind a release decision to audited evaluation inputs and the running code."""

import hashlib
from functools import lru_cache
from pathlib import Path
from uuid import UUID

from app.core.config import settings
from app.db.models import EvalRun, KnowledgeSnapshot
from app.db.session import SessionLocal
from app.services.evals.ragas_runner import REQUIRED_RAGAS_METRICS, _thresholds
from app.services.rag.snapshots import selected_snapshot


@lru_cache(maxsize=1)
def application_digest() -> str:
    root = Path(__file__).resolve().parents[2]
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*.py")):
        relative = path.relative_to(root)
        if relative.parts[0] != "tests":
            digest.update(relative.as_posix().encode())
            digest.update(hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).digest())
    return digest.hexdigest()


def gate_decision(db, run: EvalRun, *, snapshot: KnowledgeSnapshot | None = None) -> dict:
    result = run.result_json or {}
    repo_id = (result.get("target") or {}).get("repo_id")
    suite = result.get("evaluation_set") or {}
    verified = suite.get("snapshot") or {}
    reasons = []
    if snapshot is None and repo_id:
        snapshot = selected_snapshot(db, repo_id)
    if not snapshot or str(snapshot.repo_id) != repo_id or verified.get("id") != str(snapshot.id) or verified.get("sha256") != snapshot.sha256:
        reasons.append("本次评测与待发布知识快照不一致")
    if suite.get("code_sha256") != application_digest():
        reasons.append("评测代码版本与当前后端不一致")
    if suite.get("source") not in {"fixed", "published"} or not suite.get("sha256"):
        reasons.append("发布须使用版本化题集")
    if result.get("run_mode") != "full" or result.get("execution_status") != "completed" or not result.get("evaluation_complete"):
        reasons.append("缺少完整的端到端评测")
    if not result.get("passed") or not (result.get("hard_gate") or {}).get("passed"):
        reasons.append("质量或硬规则未达标")
    if (result.get("hard_gate") or {}).get("total_cases") != suite.get("case_count"):
        reasons.append("完成题数与题集不一致")
    budget = result.get("token_budget") or {}
    if budget.get("blocked"):
        reasons.append("评测受到 Token 预算拦截")
    if not budget.get("reported"):
        reasons.append("缺少有效的模型用量记录")
    expected_models = suite.get("execution_config") or {}
    if expected_models.get("answer_model") != settings.llm_model or expected_models.get("judge_model") != (settings.ragas_judge_model or settings.llm_model):
        reasons.append("当前回答模型或评分模型与评测不同")
    metrics = result.get("summary_metrics") or {}
    thresholds = _thresholds()
    for name in REQUIRED_RAGAS_METRICS:
        score = metrics.get(name)
        if not isinstance(score, (float, int)) or not thresholds[name] <= score <= 1:
            reasons.append(f"{name} 未达到 {thresholds[name]:.2f}")
    return {"eval_id": str(run.id), "allowed": not reasons, "reasons": reasons}


def verify_release_gate() -> None:
    if not settings.release_gate_eval_id:
        if settings.release_gate_required:
            raise RuntimeError("发布门禁已开启，须配置 RELEASE_GATE_EVAL_ID")
        return
    with SessionLocal() as db:
        run = db.get(EvalRun, UUID(settings.release_gate_eval_id))
        if run is None:
            raise RuntimeError("发布门禁未找到指定评测")
        decision = gate_decision(db, run)
        if not decision["allowed"]:
            raise RuntimeError("发布门禁未通过：" + "、".join(decision["reasons"]))
