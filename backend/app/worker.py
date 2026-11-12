"""Run independently: python -m app.worker [--scheduler]."""
import argparse
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from uuid import uuid4
from contextlib import suppress
from sqlalchemy import text
from app.core.config import settings
from app.core.production import validate_production_settings
from app.db.schema import verify_schema
from app.db.session import engine
from app.services.jobs import HANDLERS, job_worker_loop


async def heartbeat():
    from app.db.session import SessionLocal
    from app.db.models import ResourceLease
    from app.services.admission import _insert, release_slot
    owner = uuid4().hex
    key = "worker:" + owner
    def refresh():
        with SessionLocal() as db:
            now = datetime.now(timezone.utc)
            db.execute(_insert(db, ResourceLease).values(key=key, owner=owner, expires_at=now + timedelta(seconds=45)).on_conflict_do_update(index_elements=[ResourceLease.key], set_={"expires_at": now + timedelta(seconds=45)}))
            db.query(ResourceLease).filter(ResourceLease.key.like("worker:%"), ResourceLease.expires_at < now - timedelta(days=1)).delete(synchronize_session=False)
            db.commit()
    try:
        while True:
            await asyncio.to_thread(refresh)
            await asyncio.sleep(10)
    finally:
        with suppress(Exception):
            await asyncio.to_thread(release_slot, (key, owner))


async def run(scheduler=False):
    from app.core.telemetry import configure_logging
    configure_logging()
    from security_patches import verify_installed_patches
    verify_installed_patches()
    validate_production_settings()
    verify_schema(engine)
    if scheduler:
        from app.services.scheduler import periodic_repo_sync_loop
        # A dedicated session lock elects a single scheduler across instances.
        while True:
            with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
                postgres = engine.dialect.name == "postgresql"
                acquired = not postgres or connection.scalar(text("SELECT pg_try_advisory_lock(1129338708)"))
                if acquired:
                    try:
                        await periodic_repo_sync_loop()
                    finally:
                        if postgres:
                            connection.execute(text("SELECT pg_advisory_unlock(1129338708)"))
            await asyncio.sleep(5)
    else:
        kinds = [value.strip() for value in settings.worker_kinds.split(",") if value.strip()]
        if not kinds or not set(kinds).issubset(HANDLERS):
            raise RuntimeError("WORKER_KINDS must explicitly list approved handlers")
        await asyncio.gather(heartbeat(), *(job_worker_loop(kinds) for _ in range(settings.worker_concurrency)))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--scheduler", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try:
        asyncio.run(run(args.scheduler))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
