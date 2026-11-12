"""PostgreSQL/SQLite atomic quotas and expiring concurrency leases."""
import hashlib
import time
from datetime import datetime, timedelta, timezone
from uuid import uuid4
from sqlalchemy import case, delete, update
from app.core.config import settings
from app.db.models import RequestQuota, ResourceLease
from app.db.session import SessionLocal


def _insert(db, model):
    if db.get_bind().dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert
    return insert(model)


def consume_quota(key: str, limit: int, period: int, *, factory=None) -> bool:
    factory = factory or SessionLocal
    bucket = int(time.time()) // period
    with factory() as db:
        statement = _insert(db, RequestQuota).values(key=key, bucket=bucket, count=1).on_conflict_do_update(
            index_elements=[RequestQuota.key],
            set_={"bucket": bucket, "count": case((RequestQuota.bucket != bucket, 1), else_=RequestQuota.count + 1)},
            where=(RequestQuota.bucket != bucket) | (RequestQuota.count < limit),
        ).returning(RequestQuota.count)
        accepted = db.scalar(statement) is not None
        db.commit()
        return accepted


def acquire_slot(identity: str, limit: int, ttl: int, *, factory=None):
    factory = factory or SessionLocal
    now, owner = datetime.now(timezone.utc), uuid4().hex
    digest = hashlib.sha256(identity.encode()).hexdigest()[:40]
    with factory() as db:
        for index in range(limit):
            key = f"ai:{digest}:{index}"
            statement = _insert(db, ResourceLease).values(key=key, owner=owner, expires_at=now + timedelta(seconds=ttl)).on_conflict_do_update(
                index_elements=[ResourceLease.key], set_={"owner": owner, "expires_at": now + timedelta(seconds=ttl)},
                where=ResourceLease.expires_at <= now,
            ).returning(ResourceLease.key)
            claimed = db.scalar(statement)
            db.commit()
            if claimed:
                return key, owner
    return None


def release_slot(lease, *, factory=None):
    factory = factory or SessionLocal
    if lease:
        with factory() as db:
            db.execute(delete(ResourceLease).where(ResourceLease.key == lease[0], ResourceLease.owner == lease[1]))
            db.commit()
