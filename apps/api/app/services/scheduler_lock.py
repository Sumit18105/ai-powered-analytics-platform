from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.orm import Session


# Transaction-scoped PostgreSQL advisory locks prevent duplicate scheduled work.
# The lock is released automatically when the current DB transaction ends, so
# it cannot be accidentally released through a different pooled connection.
REFRESH_JOB_LOCK = 8_721_001
MONITORING_JOB_LOCK = 8_721_002


def try_job_lock(db: Session, key: int) -> bool:
    if db.bind is None or db.bind.dialect.name != "postgresql":
        return True
    return bool(db.scalar(text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": key}))


def release_job_lock(db: Session, key: int) -> None:
    # Kept for API compatibility; transaction-scoped locks are released by
    # COMMIT/ROLLBACK and must not be explicitly unlocked on a pooled session.
    return None
