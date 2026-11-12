"""A worker-owned subprocess; no endpoint accepts handler names or Python code."""
import asyncio
import sys
from uuid import UUID
from app.core.telemetry import configure_logging
from app.db.models import BackgroundJob, KnowledgeSourceDocument
from app.db.session import SessionLocal
from app.services.jobs import _decode, _invoke_handler


async def run(job_id, token):
    with SessionLocal() as db:
        job = db.get(BackgroundJob, job_id)
        if job is None or job.status != "running" or job.lease_token != token:
            raise RuntimeError("Job lease is no longer owned")
        kind, payload = job.kind, job.payload
    await _invoke_handler(kind, payload)
    if kind == "process_knowledge_document":
        with SessionLocal() as db:
            document = db.get(KnowledgeSourceDocument, _decode(payload["args"])[0])
            if document is not None and document.status == "failed":
                raise RuntimeError("Document indexing failed")


if __name__ == "__main__":
    import logging
    configure_logging()
    try:
        asyncio.run(run(UUID(sys.argv[1]), sys.argv[2]))
    except Exception:
        logging.getLogger(__name__).exception("Isolated job handler failed")
        raise SystemExit(1)
