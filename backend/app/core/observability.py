"""ASGI request bounds and structured audit metadata without request bodies."""
import asyncio
import json
import logging
import re
import time
from uuid import uuid4
from starlette.responses import JSONResponse
from starlette.exceptions import HTTPException
from app.core.config import settings

logger = logging.getLogger("codepilot.requests")


class RequestControls:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        state = scope.setdefault("state", {})
        request_id = uuid4().hex
        state["request_id"] = request_id
        received, status, started = 0, 500, False
        start = time.monotonic()
        headers = dict(scope.get("headers", []))
        try:
            declared = int(headers.get(b"content-length", b"0"))
        except ValueError:
            declared = -1

        async def bounded_receive():
            nonlocal received
            message = await receive()
            received += len(message.get("body", b""))
            if received > settings.request_max_bytes:
                raise BodyTooLarge()
            return message

        async def traced_send(message):
            nonlocal status, started
            if message["type"] == "http.response.start":
                status, started = message["status"], True
                message["headers"] = list(message.get("headers", [])) + [
                    (b"x-request-id", request_id.encode()), (b"x-content-type-options", b"nosniff"),
                    (b"referrer-policy", b"no-referrer"), (b"cache-control", b"no-store"),
                ]
            await send(message)

        try:
            if declared < 0 or declared > settings.request_max_bytes:
                await JSONResponse({"detail": "Request body too large", "request_id": request_id}, status_code=413)(scope, receive, traced_send)
                return
            async with asyncio.timeout(settings.request_timeout_seconds):
                await self.app(scope, bounded_receive, traced_send)
        except (BodyTooLarge, TimeoutError) as exc:
            status = 413 if isinstance(exc, BodyTooLarge) else 504
            if not started:
                await JSONResponse({"detail": "Request size or duration limit exceeded", "request_id": request_id}, status_code=status)(scope, receive, traced_send)
            else:
                await send({"type": "http.response.body", "body": b"", "more_body": False})
        except Exception:
            # Never include exception strings, prompts, credentials or query text.
            logger.error(json.dumps({"event": "request_error", "request_id": request_id}))
            if not started:
                await JSONResponse({"detail": "Internal server error", "request_id": request_id}, status_code=500)(scope, receive, traced_send)
            else:
                await send({"type": "http.response.body", "body": b"", "more_body": False})
        finally:
            from app.core.telemetry import record_request
            record_request(scope["method"], getattr(scope.get("route"), "path", "unmatched"), status, time.monotonic() - start)
            lease = state.get("admission_lease")
            if lease:
                from app.services.admission import release_slot
                try:
                    await asyncio.to_thread(release_slot, lease)
                except Exception:
                    # A database outage must not replace a completed response;
                    # the bounded lease expires without requiring a cleanup job.
                    logger.error(json.dumps({"event": "lease_release_failed", "request_id": request_id}))
            logger.info(json.dumps({"event": "request_completed", "request_id": request_id, "method": scope["method"], "route": getattr(scope.get("route"), "path", "unmatched"), "status": status, "duration_ms": round((time.monotonic() - start) * 1000)}))


class BodyTooLarge(HTTPException):
    def __init__(self):
        super().__init__(413, "Request body too large")
