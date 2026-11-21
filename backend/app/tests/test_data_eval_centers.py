import asyncio
from uuid import uuid4

import httpx
import pytest
from fastapi import BackgroundTasks, Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.routes import data_assets, evaluations
from app.core.config import settings
from app.db.models import BackgroundJob, Base, EvalCaseCheckpoint, EvalRun, Issue, Repository, RepositoryMember, User
from app.db.session import get_db
from app.services.authentication import authorize_api, token_hash


def test_catalog_and_versioned_queue_respect_repository_scope_and_budget(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, autoflush=False)

    def db_override():
        with session() as db:
            yield db

    monkeypatch.setattr(settings, "app_env", "development")
    monkeypatch.setattr(settings, "background_execution_mode", "external")
    with session.begin() as db:
        repo = Repository(owner="team", name="alpha", full_name="team/alpha")
        foreign = Repository(owner="team", name="other", full_name="team/other")
        user = User(name="manager", role="maintainer", api_token_hash=token_hash("test-token"))
        db.add_all([repo, foreign, user])
        db.flush()
        repo_id, foreign_id = str(repo.id), str(foreign.id)
        db.add_all([
            RepositoryMember(user_id=user.id, repo_id=repo.id),
            Issue(repo_id=repo.id, number=1, title="Literal 100% coverage"),
            Issue(repo_id=repo.id, number=2, title="Plain title"),
            Issue(repo_id=foreign.id, number=1, title="Foreign title"),
        ])

    app = FastAPI()
    app.dependency_overrides[get_db] = db_override
    app.include_router(data_assets.router, prefix="/api/data-assets", dependencies=[Depends(authorize_api)])
    app.include_router(evaluations.router, prefix="/api/evals", dependencies=[Depends(authorize_api)])
    client = TestClient(app, headers={"Authorization": "Bearer test-token"})

    catalog = client.get(f"/api/data-assets/{repo_id}", params={"kind": "issues", "query": "%"}).json()
    assert catalog["counts"]["issues"] == 2
    assert [item["title"] for item in catalog["items"]] == ["Literal 100% coverage"]
    assert client.get(f"/api/data-assets/{foreign_id}").status_code == 404

    cases = [{"id": "case-a", "question": "Alpha?", "reference": "A"}, {"id": "case-b", "question": "Beta?", "reference": "B"}]
    payload = {"dataset_key": "baseline", "label": "Baseline", "cases": cases}
    publish_path = f"/api/evals/datasets/{repo_id}/publish"
    first = client.post(publish_path, json=payload)
    assert first.status_code == 201, first.text
    first = first.json()
    assert client.post(f"/api/evals/datasets/{foreign_id}/publish", json=payload).status_code == 404
    second = client.post(publish_path, json={**payload, "cases": [{**cases[0], "reference": "A v2"}, cases[1]]})
    assert second.status_code == 201, second.text
    assert second.json()["version"] == 2
    assert second.json()["sha256"] != first["sha256"]
    version = client.get(f"/api/evals/datasets/{repo_id}/{first['id'].split(':')[1]}").json()
    assert version["cases"][0]["reference"] == "A"
    assert first["id"] in {item["id"] for item in client.get("/api/evals/suites", params={"repo_id": repo_id}).json()}

    run = {"repo_id": repo_id, "suite_id": first["id"], "run_judge": False}
    assert client.post("/api/evals/rag/run", json={**run, "case_budget": 1}).status_code == 422
    with session() as db:
        assert db.scalar(select(BackgroundJob)) is None
    queued = client.post("/api/evals/rag/run", json={**run, "case_budget": 2})
    assert queued.status_code == 200, queued.text
    assert queued.json()["result"]["evaluation_set"]["case_budget"] == 2

    monkeypatch.setattr(evaluations, "SessionLocal", session)
    called = []

    async def offline_case(db, repo, case, **kwargs):
        called.append(case.id)
        return {"answer": case.reference}

    from app.services.evals import ragas_runner

    monkeypatch.setattr(ragas_runner, "_run_chatagent_case", offline_case)
    from app.services.jobs import _decode

    with session() as db:
        args = _decode(db.scalar(select(BackgroundJob)).payload["args"])
    asyncio.run(evaluations._execute_rag_eval_job(*args))
    stored = client.get(f"/api/evals/{queued.json()['eval_id']}").json()["result"]
    assert [item["id"] for item in stored["cases"]] == ["case-a", "case-b"]
    with session.begin() as db:
        run_row = db.scalar(select(EvalRun))
        run_row.result_json = {**run_row.result_json, "status": "running"}
        checkpoint = db.get(EvalCaseCheckpoint, (run_row.id, "case-b"))
        checkpoint.result = None
    called.clear()
    asyncio.run(evaluations._execute_rag_eval_job(*args))
    resumed = client.get(f"/api/evals/{queued.json()['eval_id']}").json()["result"]
    assert resumed["status"] == "error"
    assert [item["id"] for item in resumed["cases"]] == ["case-a"]
    assert called == []
    engine.dispose()


def test_token_budget_blocks_before_transport_and_settles_reported_usage(monkeypatch):
    from app.db.models import EvalTokenBudget
    from app.services.evals import token_budget

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)
    monkeypatch.setattr(token_budget, "SessionLocal", session)
    with session.begin() as db:
        run = EvalRun(name="budget")
        db.add(run)
        db.flush()
        eval_id = run.id
        db.add(EvalTokenBudget(eval_id=eval_id, token_limit=1000))

    requests = []

    def provider(request):
        requests.append(request)
        return httpx.Response(200, json={"usage": {"total_tokens": 400}})

    budget = token_budget.TokenBudget(eval_id)
    payload = {"model": "deepseek-flash", "messages": [{"role": "user", "content": "Answer in one line."}], "max_tokens": 100}
    with token_budget.evaluation_budget(budget), token_budget.model_sync_client(transport=httpx.MockTransport(provider)) as client:
        url = "https://api.deepseek.com/chat/completions"
        assert client.post(url, json=payload).status_code == 200
        with pytest.raises(token_budget.TokenBudgetExceeded):
            client.post(url, json=payload)
    assert len(requests) == 1
    assert budget.summary() == {"limit": 1000, "committed": 400, "reported": 400, "blocked": True}
    engine.dispose()


def test_snapshot_selection_rollback_pinning_and_gate(monkeypatch):
    from app.db.models import Document
    from app.services.evals.ragas_runner import REQUIRED_RAGAS_METRICS
    from app.services.evals.release_gate import application_digest, gate_decision
    from app.services.rag.retrieval import search_similar_documents
    from app.services.rag.snapshots import pin_snapshot
    from app.schemas.evals import EvalRunRequest

    monkeypatch.setattr(settings, "milvus_enabled", False)
    monkeypatch.setattr(settings, "release_gate_eval_id", "")
    monkeypatch.setattr(settings, "release_gate_required", False)
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine, autoflush=False)() as db:
        repo = Repository(owner="team", name="snapshots", full_name="team/snapshots")
        owner = User(name="owner", role="owner")
        db.add_all([repo, owner])
        db.flush()
        doc = Document(repo_id=repo.id, source_id=uuid4(), source_type="knowledge_file",
                       title="Original policy", content="Rollback policy Alpha.")
        db.add(doc)
        db.commit()
        first = data_assets.save_snapshot(repo.id, data_assets.SnapshotCreate(label="Before"), db, owner)
        doc.title = "Revised policy"
        doc.content = "Rollback policy Beta."
        db.commit()
        second = data_assets.save_snapshot(repo.id, data_assets.SnapshotCreate(label="After"), db, owner)
        assert first["sha256"] != second["sha256"]

        async def run_bad_scope():
            other = Repository(owner="team", name="foreign", full_name="team/foreign")
            db.add(other)
            db.commit()
            from fastapi import HTTPException

            with pytest.raises(HTTPException) as response:
                await evaluations.run_rag_eval(
                    EvalRunRequest(repo_id=other.id, snapshot_id=first["id"]), BackgroundTasks(), db,
                )
            assert response.value.status_code == 404

        asyncio.run(run_bad_scope())

        def titles():
            return [item["title"] for item in search_similar_documents(db, repo.id, "rollback", retrieval_method="fulltext")]

        data_assets.select_snapshot(repo.id, data_assets.SnapshotSelection(snapshot_id=second["id"]), db, owner)
        assert titles() == ["Revised policy"]
        with pin_snapshot(repo.id, first["id"]):
            assert titles() == ["Original policy"]
        assert titles() == ["Revised policy"]

        run = EvalRun(name="release", result_json={
            "target": {"repo_id": str(repo.id)},
            "evaluation_set": {
                "snapshot": {"id": str(first["id"]), "sha256": first["sha256"]},
                "code_sha256": application_digest(),
                "source": "fixed", "sha256": "fixed-suite", "case_count": 10,
                "execution_config": {"answer_model": settings.llm_model, "judge_model": settings.ragas_judge_model or settings.llm_model},
            },
            "run_mode": "full", "execution_status": "completed", "evaluation_complete": True, "passed": True,
            "token_budget": {"reported": 500},
            "hard_gate": {"passed": True, "total_cases": 10},
            "summary_metrics": {metric: 1.0 for metric in REQUIRED_RAGAS_METRICS},
        })
        db.add(run)
        db.commit()
        assert gate_decision(db, run)["allowed"] is False
        data_assets.select_snapshot(repo.id, data_assets.SnapshotSelection(snapshot_id=first["id"], eval_id=run.id), db, owner)
        assert titles() == ["Original policy"]
        assert gate_decision(db, run)["allowed"] is True
        run.result_json = {**run.result_json, "summary_metrics": {**run.result_json["summary_metrics"], "answer_relevancy": 0.79}}
        assert gate_decision(db, run)["allowed"] is False
    engine.dispose()
