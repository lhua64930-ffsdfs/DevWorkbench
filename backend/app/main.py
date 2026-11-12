import asyncio

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse
from sqlalchemy import func
from contextlib import asynccontextmanager, suppress

from app.api.routes import (
    action_drafts,
    chat,
    ci,
    code_graph,
    conversations,
    evaluations,
    feedback,
    issues,
    knowledge,
    project_index,
    pull_requests,
    rag,
    repos,
    reports,
    search,
    skills,
    webhooks,
    workspaces,
)
from app.core.config import settings
from app.core.observability import RequestControls
from app.core.production import validate_production_settings
from app.db.schema import verify_schema
from app.db.doctor import database_readiness
from app.services.authentication import authorize_api, current_user
from app.services.jobs import job_worker_loop
from app.db.session import engine, get_db
from app.db.models import BackgroundJob
from app.db.models import ResourceLease
from app.services.permissions import require_permission
from app.services.scheduler import periodic_repo_sync_loop
from app.services.rag.vector_store import MilvusUnavailableError


def _cors_origins() -> list[str]:
    return [origin.strip() for origin in settings.cors_origins.split(",") if origin.strip()]


def _cors_origin_regex() -> str | None:
    if settings.app_env.lower() == "production":
        return None
    return r"^https?://(localhost|127\.0\.0\.1|\[::1\])(:\d+)?$"


@asynccontextmanager
async def application_lifespan(app):
    from app.core.telemetry import configure_logging
    configure_logging()
    from security_patches import verify_installed_patches
    verify_installed_patches()
    validate_production_settings()
    verify_schema(engine)
    tasks = []
    if settings.background_execution_mode == "embedded" and settings.app_env.lower() != "production":
        tasks.append(asyncio.create_task(job_worker_loop()))
        if settings.auto_sync_enabled:
            tasks.append(asyncio.create_task(periodic_repo_sync_loop()))
    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
        for task in tasks:
            with suppress(asyncio.CancelledError):
                await task


def create_app() -> FastAPI:
    app = FastAPI(title=settings.app_name, version="0.2.0", lifespan=application_lifespan, docs_url=None if settings.app_env == "production" else "/docs", redoc_url=None if settings.app_env == "production" else "/redoc", openapi_url=None if settings.app_env == "production" else "/openapi.json")
    app.add_middleware(RequestControls)

    @app.exception_handler(MilvusUnavailableError)
    async def milvus_unavailable_handler(_request, exc: MilvusUnavailableError) -> JSONResponse:
        return JSONResponse(
            status_code=503,
            content={"detail": str(exc), "service": "milvus", "status": "degraded"},
        )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins(),
        allow_origin_regex=_cors_origin_regex(),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(repos.router, prefix="/api/repos", tags=["repos"], dependencies=[Depends(authorize_api)])
    app.include_router(conversations.router, prefix="/api/repos", tags=["conversations"], dependencies=[Depends(authorize_api)])
    app.include_router(workspaces.router, prefix="/api/workspaces", tags=["workspaces"], dependencies=[Depends(authorize_api)])
    app.include_router(issues.router, prefix="/api", tags=["issues"], dependencies=[Depends(authorize_api)])
    app.include_router(pull_requests.router, prefix="/api", tags=["pull_requests"], dependencies=[Depends(authorize_api)])
    app.include_router(ci.router, prefix="/api", tags=["ci"], dependencies=[Depends(authorize_api)])
    app.include_router(chat.router, prefix="/api/chat", tags=["chat"], dependencies=[Depends(authorize_api)])
    app.include_router(feedback.router, prefix="/api/chat/feedback", tags=["chat_feedback"], dependencies=[Depends(authorize_api)])
    app.include_router(action_drafts.router, prefix="/api/action-drafts", tags=["action_drafts"], dependencies=[Depends(authorize_api)])
    app.include_router(reports.router, prefix="/api/reports", tags=["reports"], dependencies=[Depends(authorize_api)])
    app.include_router(search.router, prefix="/api/search", tags=["search"], dependencies=[Depends(authorize_api)])
    app.include_router(skills.router, prefix="/api/skills", tags=["skills"], dependencies=[Depends(authorize_api)])
    app.include_router(knowledge.router, prefix="/api/repos", tags=["knowledge"], dependencies=[Depends(authorize_api)])
    app.include_router(project_index.router, prefix="/api/repos", tags=["project_index"], dependencies=[Depends(authorize_api)])
    app.include_router(rag.router, prefix="/api/rag", tags=["rag"], dependencies=[Depends(authorize_api)])
    app.include_router(code_graph.router, prefix="/api", tags=["code_graph"], dependencies=[Depends(authorize_api)])
    app.include_router(evaluations.router, prefix="/api/evals", tags=["evaluations"], dependencies=[Depends(authorize_api)])
    app.include_router(webhooks.router, prefix="/api/webhooks", tags=["webhooks"])
    @app.get("/api/auth/me")
    async def auth_identity(user=Depends(current_user)):
        return {"id": str(user.id), "name": user.name, "role": user.role, "token_expires_at": user.token_expires_at}

    @app.get("/api/jobs")
    async def list_jobs(db=Depends(get_db), _user=Depends(require_permission("settings:manage"))):
        rows = db.query(BackgroundJob).order_by(BackgroundJob.created_at.desc()).limit(100).all()
        return [{"id": str(row.id), "kind": row.kind, "status": row.status, "attempts": row.attempts, "error": row.error} for row in rows]

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "app": settings.app_name}

    @app.get("/api/metrics", response_class=PlainTextResponse)
    async def metrics(db=Depends(get_db), _user=Depends(require_permission("metrics:read"))):
        from app.core.telemetry import render_metrics
        lines = render_metrics() + ["# TYPE codepilot_jobs gauge"]
        for status, count in db.query(BackgroundJob.status, func.count()).group_by(BackgroundJob.status).all():
            if status in {"queued", "running", "completed", "failed"}:
                lines.append(f'codepilot_jobs{{status="{status}"}} {count}')
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        workers = db.query(ResourceLease).filter(ResourceLease.key.like("worker:%"), ResourceLease.expires_at > now).count()
        oldest = db.query(func.min(BackgroundJob.created_at)).filter(BackgroundJob.status == "queued").scalar()
        if oldest and oldest.tzinfo is None:
            oldest = oldest.replace(tzinfo=timezone.utc)
        lines += ["# TYPE codepilot_workers_alive gauge", f"codepilot_workers_alive {workers}", "# TYPE codepilot_oldest_queued_job_seconds gauge", f"codepilot_oldest_queued_job_seconds {max(0, (now - oldest).total_seconds()) if oldest else 0}"]
        return PlainTextResponse("\n".join(lines) + "\n", media_type="text/plain; version=0.0.4")

    @app.get("/health/ready")
    def readiness() -> JSONResponse:
        from app.services.readiness import service_readiness
        result = service_readiness(engine)
        return JSONResponse(result, status_code=200 if result["status"] == "ready" else 503)


    return app


app = create_app()
