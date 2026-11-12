"""Fail-closed startup and administrator-owned outbound trust policy."""
import ipaddress
from urllib.parse import urlsplit
from app.core.config import settings


def validate_outbound_url(url: str) -> str:
    if settings.app_env.lower() != "production":
        return url
    parsed = urlsplit(url)
    allowed = {host.strip().lower() for host in settings.outbound_allowed_hosts.split(",") if host.strip()}
    if parsed.scheme != "https" or not parsed.hostname or parsed.hostname.lower() not in allowed or parsed.username or parsed.password or parsed.fragment or parsed.port not in {None, 443}:
        raise ValueError("Outbound URL must use HTTPS and an administrator-approved host")
    try:
        address = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise ValueError("Private and metadata IP destinations are prohibited")
    return url


def validate_production_settings():
    if settings.app_env.lower() != "production":
        return
    if settings.demo_mode or len(settings.token_encryption_key) < 32 or settings.token_encryption_key == "change-me-in-local-dev" or len(settings.github_webhook_secret or "") < 32:
        raise RuntimeError("Production requires DEMO_MODE=false and independent encryption/webhook secrets of at least 32 characters")
    if not settings.database_url.startswith(("postgresql:", "postgresql+")):
        raise RuntimeError("Production requires PostgreSQL")
    if settings.background_execution_mode != "external":
        raise RuntimeError("Production requires BACKGROUND_EXECUTION_MODE=external")
    origins = [origin.strip() for origin in settings.cors_origins.split(",") if origin.strip()]
    if not origins or any(urlsplit(origin).scheme != "https" or not urlsplit(origin).hostname or urlsplit(origin).path not in {"", "/"} or urlsplit(origin).username or urlsplit(origin).query or "*" in origin for origin in origins):
        raise RuntimeError("Production CORS origins must be explicit HTTPS origins")
    if settings.milvus_enabled and (not settings.milvus_token or settings.milvus_token == "root:Milvus"):
        raise RuntimeError("Production Milvus requires a rotated service identity")
    if settings.github_webhook_secret == settings.token_encryption_key:
        raise RuntimeError("Encryption and webhook secrets must be independent")
    for url in [settings.github_api_base_url, settings.llm_base_url, settings.embedding_base_url, settings.dashscope_embedding_base_url, settings.rerank_base_url, settings.ragas_judge_base_url, settings.feishu_feedback_webhook_url]:
        if url:
            validate_outbound_url(url)
