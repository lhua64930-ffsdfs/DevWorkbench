"""Run real providers on an explicitly supplied, isolated PostgreSQL database.

Only the versioned, public evaluation documents are uploaded. Secrets are loaded
from local configuration; neither credentials nor full provider errors are saved.
"""
import argparse
import asyncio
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import time
from openai import APIConnectionError
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
BASELINE = "devflow-real-project-rag-baseline"
ENTERPRISE_100 = "codepilot-enterprise-100"
BASELINE_SOURCE = "docs/datas/evals/codepilot_baseline.md"
ENTERPRISE_SOURCES = (
    BASELINE_SOURCE,
    "docs/enterprise_deployment.md",
    "docs/architecture/security.md",
    "docs/ci_sandbox.md",
    "docs/rag_and_evaluation.md",
    "docs/architecture/agent_workflow.md",
)


def write_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    temporary.replace(path)


async def evaluate(args):
    from app.core.config import settings
    from app.db.models import KnowledgeSourceDocument, Repository
    from app.db.session import SessionLocal, engine
    from app.db.schema import verify_schema
    from app.services.rag.document_processing import process_knowledge_document
    from app.services.rag.knowledge_base import get_or_create_config
    from app.services.rag.vector_store import milvus_runtime_status
    from app.services.evals.suites import load_eval_suite
    from app.services.evals.ragas_runner import _thresholds, run_rag_quality_eval
    target = make_url(settings.database_url)
    if not args.allow_isolated_database or target.get_backend_name() != "postgresql" or not str(target.database).endswith("_validation"):
        raise RuntimeError("Use an explicitly acknowledged PostgreSQL *_validation database")
    if not settings.llm_api_key or not (settings.ragas_judge_api_key or settings.llm_api_key):
        raise RuntimeError("Real model and Judge credentials are required")
    if not settings.milvus_enabled or settings.embedding_provider == "deterministic":
        raise RuntimeError("Real Milvus and non-deterministic embedding are required")
    verify_schema(engine)
    is_enterprise = args.suite == ENTERPRISE_100
    sources = [(ROOT / name) for name in (ENTERPRISE_SOURCES if is_enterprise else (BASELINE_SOURCE,))]
    digests = {source.name: hashlib.sha256(source.read_bytes()).hexdigest() for source in sources}
    repo_name = "enterprise-ragas-100" if is_enterprise else "enterprise-ragas"
    full_name = f"validation/{repo_name}"
    pending = []
    with SessionLocal() as db:
        repo = db.query(Repository).filter_by(full_name=full_name).first()
        if repo is None:
            repo = Repository(owner="validation", name=repo_name, full_name=full_name, provider="github", default_branch="main")
            db.add(repo)
            db.flush()
        config = get_or_create_config(db, repo.id, commit=False)
        config.score_threshold_enabled = False
        for source in sources:
            document = db.query(KnowledgeSourceDocument).filter_by(repo_id=repo.id, name=source.name).first()
            if document is None:
                document = KnowledgeSourceDocument(repo_id=repo.id, name=source.name, stored_path=str(source), checksum=digests[source.name], file_size=source.stat().st_size, content_type="text/markdown", status="queued")
                db.add(document)
                db.flush()
            if document.checksum != digests[source.name]:
                raise RuntimeError(f"{source.name} has changed; use a fresh isolated database")
            if document.status != "completed" or not is_enterprise:
                pending.append(document.id)
        repo_id = repo.id
        db.commit()
    for document_id in pending:
        await asyncio.to_thread(process_knowledge_document, document_id)
        with SessionLocal() as db:
            if db.get(KnowledgeSourceDocument, document_id).status != "completed":
                raise RuntimeError("Real document indexing failed; inspect local logs without publishing credentials")
    with SessionLocal() as db:
        suite = load_eval_suite(db, repo_id, args.suite)
    if is_enterprise and (len(suite.cases) != 100 or len({case.id for case in suite.cases}) != 100):
        raise ValueError("The enterprise suite must contain exactly 100 uniquely identified cases")

    destination = Path(args.output)
    checkpoint = destination.with_suffix(".checkpoint.json")
    code = hashlib.sha256()
    for path in sorted((ROOT / "backend/app").rglob("*.py")):
        if "tests" not in path.relative_to(ROOT / "backend/app").parts:
            code.update(hashlib.sha256(path.read_bytes()).digest())
    identity = {
        "suite_sha256": suite.metadata["sha256"], "documents": digests, "code_sha256": code.hexdigest(),
        "repository": str(repo_id), "collection": settings.milvus_collection,
        "model": settings.llm_model, "judge": settings.ragas_judge_model or settings.llm_model,
        "embedding": [settings.embedding_provider, settings.embedding_model, settings.embedding_dimensions],
        "strictness": settings.ragas_answer_relevancy_strictness, "thresholds": _thresholds(),
    }
    saved = json.loads(checkpoint.read_text(encoding="utf-8")) if checkpoint.exists() else {"identity": identity, "rows": {}}
    if saved["identity"] != identity:
        raise ValueError("Checkpoint inputs differ; choose a new output filename")
    rows = saved["rows"]
    resumed_cases = len(rows)

    def report_progress(stage):
        public = {"stage": stage, "completed_cases": len(rows), "total_cases": len(suite.cases)}
        write_json(destination.with_suffix(".progress.json"), public)
        print(json.dumps(public), flush=True)

    def save_case(row):
        if any("Insufficient Balance" in value for value in row["ragas_errors"].values()):
            raise RuntimeError("Model provider returned insufficient balance during scoring")
        if any("AllocationQuota.FreeTierOnly" in value for value in row["ragas_errors"].values()):
            raise RuntimeError("Alibaba Cloud Model Studio free quota exhausted during scoring")
        row["ragas_errors"] = {metric: message.split(":", 1)[0] for metric, message in row["ragas_errors"].items()}
        rows[row["id"]] = row
        write_json(checkpoint, saved)
        report_progress("case_complete")

    async def run_cases(cases):
        for attempt in range(2):
            try:
                with SessionLocal() as db:
                    pending_cases = [case for case in cases if case.id not in rows]
                    await run_rag_quality_eval(db, str(repo_id), cases=pending_cases, limit=5, run_judge=True, case_callback=save_case)
                return
            except APIConnectionError:
                if attempt:
                    raise
                await asyncio.sleep(5)

    started = time.monotonic()
    write_json(checkpoint, saved)
    report_progress("preparing")
    remaining = [case for case in suite.cases if case.id not in rows]
    async with asyncio.TaskGroup() as workers:
        for index in range(min(args.workers, len(remaining))):
            workers.create_task(run_cases(remaining[index::args.workers]))
    report_progress("finalizing")
    with SessionLocal() as db:
        result = await run_rag_quality_eval(db, str(repo_id), cases=suite.cases, limit=5, run_judge=True, evaluation_set=suite.metadata, resume_rows=rows)
    elapsed = round(time.monotonic() - started)
    result["performance"]["total_latency_ms"] = elapsed * 1000
    result["validation"] = {"date": datetime.now(timezone.utc).isoformat(), "duration_seconds": elapsed, "model": settings.llm_model, "embedding": settings.embedding_model, "vector_runtime": milvus_runtime_status(), "source_sha256": digests, "document_sha256": digests[sources[0].name], "case_count": len(suite.cases), "resumed_cases": resumed_cases, "real_model": True}
    result["validation"]["case_groups"] = dict(Counter(case.id.split("-", 1)[0] if i >= 10 else "baseline" for i, case in enumerate(suite.cases)))
    write_json(destination, result)
    print(json.dumps({"status": result.get("status"), "quality_status": result.get("quality_status"), "summary_metrics": result.get("summary_metrics"), "report": str(destination)}, ensure_ascii=False), flush=True)
    return result["passed"]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--allow-isolated-database", action="store_true")
    parser.add_argument("--suite", choices=(BASELINE, ENTERPRISE_100), default=BASELINE)
    parser.add_argument("--workers", type=int, choices=range(1, 5), default=1)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    raise SystemExit(0 if asyncio.run(evaluate(args)) else 2)


if __name__ == "__main__":
    main()
