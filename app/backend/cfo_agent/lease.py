"""Database lease for the job scheduler (see SchedulerLease)."""

from __future__ import annotations

import logging
import os
import socket
import uuid
from datetime import datetime, timedelta

from sqlalchemy import or_, update
from sqlalchemy.exc import IntegrityError

from cfo_agent.db import get_session
from cfo_agent.models import SchedulerLease

logger = logging.getLogger(__name__)

LEASE_NAME = "main"
LEASE_SECONDS = 180
OWNER_ID = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"


def acquire_or_renew(owner: str = OWNER_ID, now: datetime | None = None) -> bool:
    """Atomically take the lease if it is free/expired, or renew it if we hold it."""
    now = now or datetime.utcnow()
    session = get_session()
    try:
        res = session.execute(
            update(SchedulerLease)
            .where(SchedulerLease.name == LEASE_NAME)
            .where(or_(SchedulerLease.owner == owner, SchedulerLease.expires_at < now))
            .values(owner=owner, expires_at=now + timedelta(seconds=LEASE_SECONDS), heartbeat_at=now)
        )
        if res.rowcount == 1:
            session.commit()
            return True
        if session.get(SchedulerLease, LEASE_NAME) is None:
            session.add(SchedulerLease(
                name=LEASE_NAME, owner=owner, expires_at=now + timedelta(seconds=LEASE_SECONDS),
                heartbeat_at=now, job_runs_json={},
            ))
            try:
                session.commit()
                return True
            except IntegrityError:
                session.rollback()
        session.rollback()
        return False
    finally:
        session.close()


def holds_lease(owner: str = OWNER_ID) -> bool:
    session = get_session()
    try:
        row = session.get(SchedulerLease, LEASE_NAME)
        return bool(row and row.owner == owner and row.expires_at >= datetime.utcnow())
    finally:
        session.close()


def note_job_run(job_id: str) -> None:
    session = get_session()
    try:
        row = session.get(SchedulerLease, LEASE_NAME)
        if row is not None:
            runs = dict(row.job_runs_json or {})
            runs[job_id] = datetime.utcnow().isoformat() + "Z"
            row.job_runs_json = runs
            session.commit()
    except Exception:  # noqa: BLE001
        session.rollback()
        logger.debug("Could not record job run", exc_info=True)
    finally:
        session.close()


def lease_status() -> dict:
    session = get_session()
    try:
        row = session.get(SchedulerLease, LEASE_NAME)
        if row is None:
            return {"running": False, "owner": None, "heartbeat_at": None, "jobs": {}}
        return {
            "running": row.expires_at >= datetime.utcnow(),
            "owner": row.owner,
            "heartbeat_at": row.heartbeat_at.isoformat() + "Z" if row.heartbeat_at else None,
            "jobs": row.job_runs_json or {},
        }
    finally:
        session.close()
