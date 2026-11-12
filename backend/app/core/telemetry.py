"""Low-cardinality process metrics and credential-safe JSON logging."""
from collections import Counter
import json
import logging
import re
import threading
from app.core.config import settings

_lock = threading.Lock()
_requests = Counter()
_durations = Counter()


def record_request(method, route, status, duration):
    key = (method if method in {"GET", "POST", "PATCH", "DELETE", "PUT", "HEAD", "OPTIONS"} else "OTHER", route, status)
    with _lock:
        _requests[key] += 1
        _durations[key] += duration


def render_metrics():
    lines = ["# TYPE codepilot_http_requests_total counter", "# TYPE codepilot_http_duration_seconds_total counter"]
    with _lock:
        for (method, route, status), count in _requests.items():
            labels = f'method={json.dumps(method)},route={json.dumps(route)},status="{status}"'
            lines += [f"codepilot_http_requests_total{{{labels}}} {count}", f"codepilot_http_duration_seconds_total{{{labels}}} {_durations[(method, route, status)]:.6f}"]
    return lines


def redact(message):
    for field in ("llm_api_key", "embedding_api_key", "ragas_judge_api_key", "github_token", "dashscope_api_key", "rerank_api_key", "milvus_token", "token_encryption_key", "github_webhook_secret", "feishu_feedback_webhook_url"):
        secret = getattr(settings, field, None)
        if secret and len(secret) >= 8:
            message = message.replace(secret, "[REDACTED]")
    message = re.sub(r"(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{30,}|sk-[A-Za-z0-9_-]{20,})", "[REDACTED]", message)
    message = re.sub(r"(?i)(?:Bearer|Basic)\s+[A-Za-z0-9+/=_\-.]+", "[REDACTED AUTH]", message)
    message = re.sub(r"(https?://)[^\s/@]+:[^\s/@]+@", r"\1[REDACTED]@", message)
    return message


class JsonFormatter(logging.Formatter):
    def format(self, record):
        result = {"level": record.levelname, "logger": record.name, "message": redact(record.getMessage())}
        if record.exc_info:
            result["exception_type"] = record.exc_info[0].__name__
        return json.dumps(result, ensure_ascii=False)


def configure_logging():
    if settings.app_env.lower() == "production":
        handler = logging.StreamHandler()
        handler.setFormatter(JsonFormatter())
        logging.getLogger().handlers[:] = [handler]
        logging.getLogger().setLevel(logging.INFO)
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)
