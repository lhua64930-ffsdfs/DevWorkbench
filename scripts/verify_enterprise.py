"""Real PostgreSQL, two HTTP processes and two worker process acceptance checks.

Run with DATABASE_URL pointing only to a disposable *_validation database.
Never creates users/jobs in a business database; never contacts GitHub or an LLM.
"""
import argparse
import asyncio
from collections import Counter
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import time
from uuid import UUID, uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))


def child(mode, identity):
    from app.db.session import SessionLocal
    if mode == "quota":
        from app.services.admission import consume_quota
        print(json.dumps({"accepted": sum(consume_quota(identity, 10, 3600) for _ in range(10))}))
    elif mode == "slots":
        from app.services.admission import acquire_slot
        print(json.dumps({"accepted": int(acquire_slot(identity, 2, 60) is not None)}))
    elif mode == "lock":
        from app.services.repo_lock import repository_sync_lock
        from fastapi import HTTPException
        async def claim():
            with SessionLocal() as db:
                try:
                    async with repository_sync_lock(db, UUID(identity)):
                        print("acquired", flush=True)
                        await asyncio.sleep(4)
                except HTTPException as exc:
                    print(str(exc.status_code), flush=True)
        asyncio.run(claim())


async def verify(args):
    from sqlalchemy import text
    from sqlalchemy.engine import make_url
    target = make_url(os.environ["DATABASE_URL"])
    if not args.allow_isolated_database or not str(target.database).endswith("_validation") or not target.drivername.startswith("postgresql"):
        raise RuntimeError("Explicitly acknowledge an isolated PostgreSQL *_validation database")
    # Avoid implicit use of developer credentials or periodic external sync.
    os.environ.update(APP_ENV="production", DEMO_MODE="false", BACKGROUND_EXECUTION_MODE="external", AUTO_SYNC_ENABLED="false", MILVUS_ENABLED="false", LLM_API_KEY="", RAG_LLM_ENABLED="false", EMBEDDING_PROVIDER="deterministic", TOKEN_ENCRYPTION_KEY=secrets.token_urlsafe(48), GITHUB_WEBHOOK_SECRET=secrets.token_urlsafe(48), CORS_ORIGINS="https://validation.local", WORKER_KINDS="scan_project_index_by_id", WORKER_CONCURRENCY="1", WORKER_POLL_SECONDS="0.1", REQUEST_MAX_BYTES="4096", REQUESTS_PER_MINUTE="100", PYTHONUNBUFFERED="1")
    from app.db.models import ActionDraft, AuditLog, BackgroundJob, Repository, RepositoryMember, User
    from app.db.session import SessionLocal, engine
    from app.services.authentication import token_hash
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=ROOT / "backend", check=True)
    label = uuid4().hex[:10]
    tokens = {name: secrets.token_urlsafe(32) for name in ("owner", "member", "quota", "expired", "disabled", "legacy")}
    with SessionLocal() as db:
        own, other = [Repository(owner="validation", name=f"{name}-{label}", full_name=f"validation/{name}-{label}") for name in ("own", "other")]
        db.add_all([own, other])
        users = {name: User(name=f"{name}-{label}", role="owner" if name == "owner" else "maintainer", api_token_hash=token_hash(token), token_expires_at=None if name == "legacy" else datetime.now(timezone.utc) + timedelta(days=-1 if name == "expired" else 1), disabled=name == "disabled") for name, token in tokens.items()}
        db.add_all(users.values())
        db.flush()
        db.add(RepositoryMember(user_id=users["member"].id, repo_id=own.id))
        draft = ActionDraft(repo_id=own.id, draft_type="send_report", target_type="repository", content="Synthetic validation only")
        db.add(draft)
        db.commit()
        own_id, other_id, draft_id = str(own.id), str(other.id), str(draft.id)
    env = os.environ.copy()
    processes, streams = [], []
    report = {"date_utc": datetime.now(timezone.utc).isoformat(), "database": "PostgreSQL", "checks": {}}
    results = report["checks"]
    directory = Path(args.output).resolve().parent
    directory.mkdir(parents=True, exist_ok=True)
    def start(command, name):
        stream = open(directory / f"{name}.log", "wb")
        streams.append(stream)
        process = subprocess.Popen(command, cwd=ROOT / "backend", env=env, stdout=stream, stderr=subprocess.STDOUT)
        processes.append(process)
        return process
    def helper(mode, identity):
        return subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--child", mode, "--identity", identity], cwd=ROOT / "backend", env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    import httpx
    try:
        ports = [args.port, args.port + 1]
        for index, port in enumerate(ports):
            start([sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port), "--no-access-log"], f"api-{index}")
        async with httpx.AsyncClient(timeout=20, trust_env=False) as client:
            bases = [f"http://127.0.0.1:{port}" for port in ports]
            for base in bases:
                for _ in range(100):
                    try:
                        if (await client.get(base + "/health/ready")).status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    await asyncio.sleep(.2)
                else:
                    raise RuntimeError("An HTTP process did not become ready")
            def headers(name): return {"Authorization": "Bearer " + tokens[name]}
            assert (await client.get(bases[0] + "/api/repos")).status_code == 401
            for name in ("expired", "disabled", "legacy"):
                assert (await client.get(bases[1] + "/api/auth/me", headers=headers(name))).status_code == 401
            results["token_expiry_disable_legacy_rejection"] = True
            rows = (await client.get(bases[0] + "/api/repos", headers=headers("member"))).json()
            assert [r["id"] for r in rows] == [own_id]
            response = await client.post(bases[1] + f"/api/repos/{other_id}/sync", json={"repo_id": own_id}, headers=headers("member"))
            assert response.status_code == 404
            results["repository_isolation_and_path_body_spoof"] = True
            assert (await client.get(bases[0] + "/openapi.json")).status_code == 404
            assert (await client.get(bases[0] + "/api/metrics")).status_code == 401
            assert (await client.get(bases[1] + "/api/metrics", headers=headers("member"))).status_code == 403
            assert (await client.get(bases[0] + "/api/metrics", headers=headers("owner"))).status_code == 200
            results["protected_metrics_and_disabled_docs"] = True
            large = await client.post(bases[0] + "/api/chat", content=b"x" * 5000, headers=headers("owner"))
            assert large.status_code == 413 and large.headers.get("x-request-id")
            results["body_limit"] = True
            responses = await asyncio.gather(*(client.post(bases[i % 2] + f"/api/action-drafts/{draft_id}/confirm", headers=headers("owner")) for i in range(30)))
            counts = Counter(r.status_code for r in responses)
            assert counts[200] == 1 and all(code in {200, 400, 409} for code in counts), counts
            with SessionLocal() as db:
                assert db.query(AuditLog).filter_by(target_id=draft_id, action="action_draft.confirm.send_report").count() == 1
            results["two_api_30_confirmations"] = {"success": 1, "duplicates_rejected": 29, "audit_rows": 1}
            # Sequential requests cross both processes while sharing one DB quota.
            # Align away from a fixed-window boundary so the proof is reproducible.
            while time.time() % 60 > 45:
                await asyncio.sleep(.5)
            codes = [(await client.get(bases[i % 2] + "/api/repos", headers=headers("quota"))).status_code for i in range(101)]
            assert codes.count(200) == 100 and codes[-1] == 429, Counter(codes)
            results["two_api_shared_request_quota"] = {"accepted": 100, "rejected": 1}
        quota_identity = "validation:" + label
        children = [helper("quota", quota_identity) for _ in range(4)]
        accepted = 0
        for process in children:
            out, err = process.communicate(timeout=40)
            assert process.returncode == 0, "Quota subprocess failed"
            accepted += json.loads(out)["accepted"]
        assert accepted == 10
        results["four_process_atomic_quota"] = {"accepted": accepted, "attempted": 40}
        children = [helper("slots", quota_identity) for _ in range(5)]
        accepted = sum(json.loads(p.communicate(timeout=40)[0])["accepted"] for p in children)
        assert accepted == 2
        results["five_process_concurrency_slots"] = {"accepted": 2, "rejected": 3}
        first = helper("lock", own_id)
        assert first.stdout.readline().strip() == "acquired"
        second = helper("lock", own_id)
        assert second.communicate(timeout=20)[0].strip() == "409"
        first.communicate(timeout=20)
        third = helper("lock", own_id)
        assert third.communicate(timeout=20)[0].strip() == "acquired"
        results["cross_process_repository_lock_and_release"] = True
        with SessionLocal() as db:
            missing_repo = {"$uuid": str(uuid4())}
            jobs = [BackgroundJob(kind="scan_project_index_by_id", payload={"args": [missing_repo]}) for _ in range(20)]
            recovered = BackgroundJob(kind="scan_project_index_by_id", payload={"args": [missing_repo]}, status="running", attempts=1, lease_token="terminated-worker", lease_until=datetime.now(timezone.utc) - timedelta(seconds=2))
            db.add_all([*jobs, recovered])
            db.commit()
            job_ids, recovered_id = [job.id for job in jobs], recovered.id
        for index in range(2):
            start([sys.executable, "-m", "app.worker"], f"worker-{index}")
        for _ in range(200):
            with SessionLocal() as db:
                rows = db.query(BackgroundJob).filter(BackgroundJob.id.in_([*job_ids, recovered_id])).all()
                if all(row.status == "completed" for row in rows):
                    assert all(row.attempts == (2 if row.id == recovered_id else 1) for row in rows)
                    break
            await asyncio.sleep(.5)
        else:
            raise RuntimeError("Independent worker recovery did not finish")
        results["two_worker_claims_and_expired_lease_recovery"] = {"completed_once": 20, "recovered_on_attempt": 2, "isolated_subprocess_execution": True}
        with engine.connect() as db:
            report["postgres_version"] = db.scalar(text("SHOW server_version"))
        report["passed"] = True
        Path(args.output).write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps(report), flush=True)
    finally:
        for process in reversed(processes):
            if process.poll() is None:
                process.terminate()
        for process in processes:
            try: process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        for stream in streams:
            stream.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-isolated-database", action="store_true")
    parser.add_argument("--output")
    parser.add_argument("--port", type=int, default=18810)
    parser.add_argument("--child", choices=["quota", "slots", "lock"])
    parser.add_argument("--identity")
    args = parser.parse_args()
    if args.child:
        child(args.child, args.identity)
    elif not args.output:
        parser.error("--output is required")
    else:
        asyncio.run(verify(args))


if __name__ == "__main__":
    main()
