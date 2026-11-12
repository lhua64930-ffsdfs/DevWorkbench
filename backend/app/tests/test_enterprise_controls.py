import asyncio
from datetime import datetime, timedelta, timezone
import importlib.util
from pathlib import Path
import uuid

import httpx
import pytest
from fastapi import FastAPI, Request
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.config import settings
from app.core.observability import RequestControls
from app.core.production import validate_outbound_url, validate_production_settings
from app.core.security import encrypt_token, decrypt_token
from app.db.models import Base, Repository, RepositoryMember, User, ResourceLease
from app.db.session import get_db
from app.main import create_app
from app.services import admission
from app.services.authentication import token_hash


@pytest.mark.parametrize("path", ["/api/issues/1/analyze/stream", "/api/pull-requests/1/review-checklist", "/api/reports/weekly", "/api/workspaces/multi-repo-report", "/api/rag/repositories/1/documents", "/api/repos/1/knowledge/upload"])
def test_streaming_and_batch_routes_share_ai_admission(path):
    from app.services.authentication import is_ai_request
    assert is_ai_request("POST", path)
    assert not is_ai_request("GET", path)
    assert not is_ai_request("POST", "/api/chat/feedback")


@pytest.mark.asyncio
async def test_monitor_token_cannot_access_business_data(isolated):
    app, factory = isolated
    with factory() as db:
        db.add(User(name="monitor", role="monitor", api_token_hash=token_hash("metrics-only-token"), token_expires_at=datetime.now(timezone.utc) + timedelta(days=1)))
        db.commit()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", headers={"Authorization": "Bearer metrics-only-token"}) as client:
        assert (await client.get("/api/metrics")).status_code == 200
        assert (await client.get("/api/repos")).status_code == 403
        assert (await client.get("/api/jobs")).status_code == 403


def test_production_workspace_rejects_legacy_external_paths(monkeypatch, tmp_path):
    from app.services.workspace_tools import _workspace_root
    monkeypatch.setattr(settings, "app_env", "production")
    monkeypatch.setattr(settings, "repo_checkout_dir", str(tmp_path / "managed"))
    repo = Repository(id=uuid.uuid4(), owner="t", name="r", full_name="t/r", local_path=str(tmp_path / "outside"))
    with pytest.raises(ValueError):
        _workspace_root(repo)
    with pytest.raises(ValueError):
        _workspace_root(None)


def test_vector_failure_is_redacted_in_readiness(monkeypatch):
    from app.services import readiness
    import pymilvus
    monkeypatch.setattr(settings, "milvus_enabled", True)
    monkeypatch.setattr(readiness, "database_readiness", lambda engine: {"status": "ready"})
    def unavailable(**kwargs):
        raise RuntimeError("private-provider-token-and-url")
    monkeypatch.setattr(pymilvus, "MilvusClient", unavailable)
    result = readiness.service_readiness(None)
    assert result["status"] == "unavailable" and result["vector_store"] == "unavailable"
    assert "private-provider-token" not in str(result)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["provider_error", "empty_answer", "truncated_answer"])
async def test_failed_grounding_uses_only_retrieved_evidence(monkeypatch, failure):
    from app.services.agents.chat_agent import ChatAgent
    from langchain_core.messages import AIMessage
    from langchain_openai import ChatOpenAI

    monkeypatch.setattr(settings, "grounded_answers_enabled", True)
    model = ChatOpenAI(model="test", api_key="unit-test-no-network")

    async def fail(*args, **kwargs):
        if failure == "provider_error":
            raise RuntimeError("private-provider-detail")
        if failure == "truncated_answer":
            return AIMessage(content="Unverified partial text", response_metadata={"finish_reason": "length"})
        return AIMessage(content="")

    monkeypatch.setattr(ChatOpenAI, "ainvoke", fail)
    state = {
        "message": "How are writes approved?",
        "grounding_evidence": [{"title": "Write policy", "snippet": "All writes require manual approval."}],
    }
    answer = await ChatAgent(model=model)._ground_answer("Unverified claim: all writes run automatically", state)
    assert "all writes run automatically" not in answer
    assert "Unverified partial text" not in answer
    assert "private-provider-detail" not in answer
    assert "复核未完成" in answer
    assert "Write policy" in answer
    assert "All writes require manual approval." in answer
    assert state["agent_events"][-1]["reason"] == failure


@pytest.fixture
def isolated(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(admission, "SessionLocal", factory)
    monkeypatch.setattr(settings, "app_env", "production")
    monkeypatch.setattr(settings, "demo_mode", False)
    app = create_app()
    def dependency():
        with factory() as db:
            yield db
    app.dependency_overrides[get_db] = dependency
    yield app, factory
    engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["expired", "disabled", "legacy", "valid"])
async def test_production_token_lifecycle(isolated, state):
    app, factory = isolated
    with factory() as db:
        db.add(User(name="operator", api_token_hash=token_hash("test-personal-token"), disabled=state == "disabled", token_expires_at=None if state == "legacy" else datetime.now(timezone.utc) + timedelta(days=-1 if state == "expired" else 1)))
        db.commit()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/auth/me", headers={"Authorization": "Bearer test-personal-token"})
        assert response.status_code == (200 if state == "valid" else 401)
        assert "test-personal-token" not in response.text
        assert (await client.get("/openapi.json")).status_code == 404


@pytest.mark.parametrize("url", ["http://api.github.com", "https://api.github.com.evil.example", "https://api.github.com@127.0.0.1", "https://api.github.com:8443", "file:///etc/passwd", "https://169.254.169.254/latest/meta-data", "https://localhost"])
def test_production_egress_policy_rejects_untrusted_destinations(monkeypatch, url):
    monkeypatch.setattr(settings, "app_env", "production")
    with pytest.raises(ValueError):
        validate_outbound_url(url)
    assert validate_outbound_url("https://api.github.com/repos/example/repo")


def test_startup_rejects_unsafe_defaults(monkeypatch):
    monkeypatch.setattr(settings, "app_env", "production")
    monkeypatch.setattr(settings, "demo_mode", True)
    with pytest.raises(RuntimeError):
        validate_production_settings()


@pytest.mark.asyncio
async def test_body_cannot_override_repository_path(isolated):
    app, factory = isolated
    with factory() as db:
        mine = Repository(owner="t", name="mine", full_name="t/mine")
        other = Repository(owner="t", name="other", full_name="t/other")
        user = User(name="dev", role="maintainer", api_token_hash=token_hash("test-dev-token"), token_expires_at=datetime.now(timezone.utc) + timedelta(days=1))
        db.add_all([mine, other, user])
        db.flush()
        db.add(RepositoryMember(user_id=user.id, repo_id=mine.id))
        db.commit()
        mine_id, other_id = str(mine.id), str(other.id)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", headers={"Authorization": "Bearer test-dev-token"}) as client:
        response = await client.post(f"/api/repos/{other_id}/sync", json={"repo_id": mine_id})
        assert response.status_code == 404


@pytest.mark.asyncio
async def test_streaming_body_bound_and_timeout(monkeypatch):
    monkeypatch.setattr(settings, "request_max_bytes", 1024)
    monkeypatch.setattr(settings, "request_timeout_seconds", 0.02)
    app = FastAPI()
    app.add_middleware(RequestControls)
    @app.post("/body")
    async def body(request: Request):
        return {"length": len(await request.body())}
    @app.get("/slow")
    async def slow():
        await asyncio.sleep(0.1)
        return {}
    async def chunks():
        yield b"a" * 800
        yield b"b" * 800
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/body", content=chunks())
        assert response.status_code == 413
        assert response.headers["x-request-id"]
        assert (await client.get("/slow")).status_code == 504


def test_stale_slot_owner_cannot_release_new_lease(isolated):
    _, factory = isolated
    first = admission.acquire_slot("same-user", 1, 30)
    assert admission.acquire_slot("same-user", 1, 30) is None
    with factory() as db:
        db.get(ResourceLease, first[0]).expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        db.commit()
    second = admission.acquire_slot("same-user", 1, 30)
    assert second and second != first
    admission.release_slot(first)
    assert admission.acquire_slot("same-user", 1, 30) is None
    admission.release_slot(second)
    assert admission.acquire_slot("same-user", 1, 30)


def test_key_rotation_retains_old_ciphertexts_only_during_overlap(monkeypatch):
    old = encrypt_token("private-github-token", "old-random-key")
    monkeypatch.setattr(settings, "token_encryption_previous_keys", "old-random-key")
    assert decrypt_token(old, "new-random-key") == "private-github-token"
    new = encrypt_token(decrypt_token(old, "new-random-key"), "new-random-key")
    monkeypatch.setattr(settings, "token_encryption_previous_keys", "")
    assert decrypt_token(old, "new-random-key") is None
    assert decrypt_token(new, "new-random-key") == "private-github-token"


def test_backup_authentication_rejects_tampering_before_use(tmp_path):
    source = Path(__file__).resolve().parents[3] / "scripts/backup_restore.py"
    spec = importlib.util.spec_from_file_location("backup_restore", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    plain, encrypted, result = tmp_path / "plain", tmp_path / "backup", tmp_path / "result"
    plain.write_bytes(b"private-upload" * 1000)
    key = b"k" * 32
    module.encrypt_file(plain, encrypted, key)
    module.decrypt_file(encrypted, result, key)
    assert result.read_bytes() == plain.read_bytes()
    damaged = bytearray(encrypted.read_bytes())
    damaged[40] ^= 1
    encrypted.write_bytes(damaged)
    from cryptography.exceptions import InvalidTag
    with pytest.raises(InvalidTag):
        module.decrypt_file(encrypted, tmp_path / "damaged-result", key)
