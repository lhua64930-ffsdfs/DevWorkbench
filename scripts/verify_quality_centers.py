"""Exercise a snapshot-bound evaluation with real DeepSeek Flash."""

import argparse
import asyncio
import hashlib
import json
import os
import sys
from pathlib import Path

from sqlalchemy.engine import make_url

from run_real_ragas import BASELINE, ENTERPRISE_100, BASELINE_SOURCE, ENTERPRISE_SOURCES, ROOT, write_json


async def run(args):
    from fastapi import BackgroundTasks

    from app.api.routes.evaluations import _execute_rag_eval_job, run_rag_eval
    from app.core.config import settings
    from app.db.models import BackgroundJob, EvalRun, KnowledgeSourceDocument, Repository
    from app.db.schema import verify_schema
    from app.db.session import SessionLocal, engine
    from app.schemas.evals import EvalRunRequest
    from app.services.evals.release_gate import gate_decision
    from app.services.jobs import _decode
    from app.services.rag.document_processing import process_knowledge_document
    from app.services.rag.knowledge_base import get_or_create_config
    from app.services.rag.snapshots import create_snapshot

    target = make_url(settings.database_url)
    if target.get_backend_name() != "postgresql" or not str(target.database).endswith("_validation"):
        raise RuntimeError("Use an isolated PostgreSQL *_validation database")
    verify_schema(engine)
    source_paths = ENTERPRISE_SOURCES if args.suite == ENTERPRISE_100 else (BASELINE_SOURCE,)
    with SessionLocal.begin() as db:
        name = "enterprise-ragas-100" if args.suite == ENTERPRISE_100 else "enterprise-ragas"
        repo = Repository(owner="validation", name=name, full_name=f"validation/{name}")
        db.add(repo)
        db.flush()
        repo_id = repo.id
        config = get_or_create_config(db, repo_id, commit=False)
        config.score_threshold_enabled = False
        sources = []
        for path in (ROOT / value for value in source_paths):
            raw = path.read_bytes()
            source = KnowledgeSourceDocument(repo_id=repo_id, name=path.name, stored_path=str(path),
                                             checksum=hashlib.sha256(raw).hexdigest(), file_size=len(raw))
            db.add(source)
            sources.append(source)
        db.flush()
        ids = [source.id for source in sources]

    for source_id in ids:
        await asyncio.to_thread(process_knowledge_document, source_id)
        with SessionLocal() as db:
            if db.get(KnowledgeSourceDocument, source_id).status != "completed":
                raise RuntimeError("Public evaluation document indexing failed")
    with SessionLocal() as db:
        snapshot = create_snapshot(db, repo_id, "Public evaluation corpus")
        snapshot_id = snapshot.id
        db.commit()
        queued = await run_rag_eval(
            EvalRunRequest(repo_id=repo_id, name="DeepSeek Flash quality-center verification", suite_id=args.suite,
                           token_budget=args.token_budget, snapshot_id=snapshot_id, run_judge=True),
            BackgroundTasks(), db,
        )
        job = db.query(BackgroundJob).filter_by(kind="_execute_rag_eval_job").one()
        job_args = _decode(job.payload["args"])
        print(json.dumps({"started": queued.eval_id, "cases": queued.result["evaluation_set"]["case_count"],
                          "answer": settings.llm_model, "judge": settings.ragas_judge_model,
                          "vector_chunks": snapshot.vector_count, "token_limit": args.token_budget}), flush=True)

    await _execute_rag_eval_job(*job_args)
    with SessionLocal() as db:
        run = db.query(EvalRun).one()
        result = run.result_json
        decision = gate_decision(db, run, snapshot=db.get(type(snapshot), snapshot_id))
    write_json(Path(args.output), {"result": result, "release_gate": decision})
    print(json.dumps({"status": result.get("status"), "quality": result.get("quality_status"),
                      "metrics": result.get("summary_metrics"), "tokens": result.get("token_budget"),
                      "cases": len(result.get("cases") or []), "gate": decision}, ensure_ascii=True), flush=True)
    return 0 if decision["allowed"] else 2


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--suite", choices=(BASELINE, ENTERPRISE_100), default=BASELINE)
    parser.add_argument("--token-budget", required=True, type=int)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    api_key = os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("LLM_API_KEY")
    if not api_key or not os.environ.get("DASHSCOPE_API_KEY"):
        raise SystemExit("Set DEEPSEEK_API_KEY and DASHSCOPE_API_KEY in the environment")
    os.environ.update({
        "LLM_API_KEY": api_key, "RAGAS_JUDGE_API_KEY": api_key,
        "LLM_MODEL": "deepseek-flash", "RAGAS_JUDGE_MODEL": "deepseek-flash",
        "LLM_BASE_URL": "https://api.deepseek.com", "RAGAS_JUDGE_BASE_URL": "https://api.deepseek.com",
        "EMBEDDING_PROVIDER": "dashscope", "EMBEDDING_MODEL": "text-embedding-v4", "EMBEDDING_DIMENSIONS": "1024",
        "DASHSCOPE_EMBEDDING_BASE_URL": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "RERANK_BASE_URL": "", "FEISHU_FEEDBACK_WEBHOOK_URL": "", "GITHUB_TOKEN": "",
        "BACKGROUND_EXECUTION_MODE": "external", "AUTO_SYNC_ENABLED": "false",
        "MAX_EVAL_CASES": "100", "MAX_EVAL_TOKENS": str(args.token_budget),
    })
    sys.exit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
