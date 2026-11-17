"""Prometheus metrics at /metrics: queue depth, workers, digest freshness, AI spend, open
alerts and backup age, per tenant. Off (404) unless METRICS_TOKEN is set; scrapers send
`Authorization: Bearer <token>`. Point the scraper at an internal address: /metrics is not
tied to a tenant host, it reports every tenant."""

from __future__ import annotations

import hmac
import logging
from datetime import datetime, timezone

from sqlalchemy import func

logger = logging.getLogger(__name__)


def _esc(v: str) -> str:
    return str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


class _Out:
    def __init__(self) -> None:
        self.lines: list[str] = []
        self._seen: set[str] = set()

    def add(self, name: str, help_: str, value: float, **labels: str) -> None:
        if name not in self._seen:
            self._seen.add(name)
            self.lines += [f"# HELP {name} {help_}", f"# TYPE {name} gauge"]
        lab = ",".join(f'{k}="{_esc(v)}"' for k, v in labels.items())
        self.lines.append(f"{name}{{{lab}}} {value:g}" if lab else f"{name} {value:g}")


def _collect_one(out: _Out, tenant: str) -> None:
    from cfo_agent import backup, jobqueue
    from cfo_agent.budgetmeter import monthly_budget_usd, spend_this_month
    from cfo_agent.db import get_session
    from cfo_agent.models import Digest, JobWorker, OpsAlert, PipelineRun

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    s = get_session()
    try:
        for status, n in jobqueue.counts().items():
            out.add("cfo_jobs", "Background jobs by status.", n, tenant=tenant, status=status)
        workers = [w for w in s.query(JobWorker).all() if (now - w.heartbeat_at).total_seconds() < 120]
        out.add("cfo_workers_alive", "Job workers seen in the last two minutes.", len(workers), tenant=tenant)
        last = s.query(func.max(Digest.created_at)).scalar()
        if last:
            out.add("cfo_last_digest_age_seconds", "Seconds since the latest digest was created.",
                    (now - last).total_seconds(), tenant=tenant)
        from datetime import timedelta

        failed = s.query(func.count(PipelineRun.id)).filter(
            PipelineRun.created_at >= now - timedelta(days=7), PipelineRun.status.in_(["failed", "data_unavailable"])).scalar()
        out.add("cfo_digest_runs_failed_7d", "Digest runs that failed or had no data in the last 7 days.", failed or 0, tenant=tenant)
        for kind, usd in spend_this_month(s, now).items():
            out.add("cfo_ai_spend_month_usd", "AI spend this calendar month, USD.", usd, tenant=tenant, kind=kind)
        out.add("cfo_ai_budget_month_usd", "Monthly AI budget, USD (0 = none).", monthly_budget_usd(), tenant=tenant)
        for sev in ("warn", "critical"):
            n = s.query(func.count(OpsAlert.id)).filter(OpsAlert.resolved_at.is_(None), OpsAlert.severity == sev).scalar()
            out.add("cfo_alerts_open", "Open operator alerts.", n or 0, tenant=tenant, severity=sev)
        age = backup.last_backup_age_hours()
        if age is not None:
            out.add("cfo_backup_age_hours", "Hours since the newest verified backup.", age, tenant=tenant)
    finally:
        s.close()


def render() -> str:
    from cfo_agent.tenancy import current_tenant_id, for_each_tenant

    out = _Out()

    def one():
        _collect_one(out, current_tenant_id() or "default")

    for_each_tenant(one)
    return "\n".join(out.lines) + "\n"


def authorized(header: str | None, token: str | None) -> bool:
    if not token or not header or not header.lower().startswith("bearer "):
        return False
    return hmac.compare_digest(header[7:].strip().encode(), token.encode())
