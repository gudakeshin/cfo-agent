"""Operator alerts: tell a person when something that needs one has gone wrong.

`raise_alert(key, ...)` records a condition and notifies once; while it lasts it repeats
every ALERT_REPEAT_HOURS (not every check); `resolve_alert(key)` closes it and says so.
Channels, both optional (with neither, alerts are logged and shown in the ops view):

  ALERT_WEBHOOK_URL   Slack or Microsoft Teams incoming webhook (a JSON {"text": ...} POST)
  ALERT_EMAIL_TO      comma-separated addresses, sent with the app's own SMTP / Resend setup

The channel is the platform operator's, shared by every tenant, so each message names the
tenant. Messages carry the condition and an error summary, never client figures. Delivery
failures are logged and never raised: alerting must not be able to break the thing it watches."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from cfo_agent.db import get_session
from cfo_agent.models import OpsAlert
from cfo_agent.settings import get_settings

logger = logging.getLogger(__name__)

SEVERITIES = ("warn", "critical")


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _tenant_label() -> str:
    from cfo_agent.tenancy import current_tenant_id

    return current_tenant_id() or "default"


# ── delivery ─────────────────────────────────────────────────────────────────

def _format(prefix: str, severity: str, title: str, detail: str) -> str:
    mark = {"critical": "🔴", "warn": "🟠"}.get(severity, "•") if prefix != "RESOLVED" else "✅"
    head = f"{mark} [{prefix}] {_tenant_label()}: {title}"
    return head + (f"\n{detail}" if detail else "")


def _send_webhook(url: str, text: str) -> bool:
    import httpx

    from cfo_agent.secret_refs import resolve_secret

    try:
        r = httpx.post(resolve_secret(url), json={"text": text}, timeout=10)
        r.raise_for_status()
        return True
    except Exception as e:  # noqa: BLE001
        logger.error("Alert webhook failed: %s", type(e).__name__)
        return False


def _send_email(addresses: list[str], subject: str, text: str) -> bool:
    from cfo_agent.digest import email as mail

    html = "<pre style=\"font:14px/1.5 -apple-system,Segoe UI,sans-serif;white-space:pre-wrap\">" + (
        text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")) + "</pre>"
    try:
        return bool(mail._send_resend(html, subject, addresses, text) or mail._send_smtp(html, subject, addresses, text))
    except Exception as e:  # noqa: BLE001
        logger.error("Alert email failed: %s", type(e).__name__)
        return False


def deliver(prefix: str, severity: str, title: str, detail: str) -> bool:
    """Send to every configured channel. True if at least one took it."""
    s = get_settings()
    text = _format(prefix, severity, title, detail)
    logger.log(logging.ERROR if severity == "critical" else logging.WARNING, "ALERT %s", text.replace("\n", " | "))
    ok = False
    if s.alert_webhook_url:
        ok = _send_webhook(s.alert_webhook_url, text) or ok
    addresses = [a.strip() for a in s.alert_email_to.split(",") if a.strip()]
    if addresses:
        ok = _send_email(addresses, f"[{prefix}] {_tenant_label()}: {title}", text) or ok
    return ok


# ── state ────────────────────────────────────────────────────────────────────

def raise_alert(key: str, severity: str, title: str, detail: str = "") -> bool:
    """Record the condition and notify if it is new, or last notified long enough ago.
    Returns True when a notification went out."""
    if severity not in SEVERITIES:
        raise ValueError(f"severity must be one of {', '.join(SEVERITIES)}")
    s = get_session()
    try:
        now = _now()
        row = s.query(OpsAlert).filter(OpsAlert.key == key, OpsAlert.resolved_at.is_(None)).first()
        if row is None:
            row = OpsAlert(key=key, severity=severity, title=title, detail=detail[:2000], notify_count=0)
            s.add(row)
            due = True
        else:
            escalated = severity == "critical" and row.severity != "critical"
            row.severity, row.title, row.detail, row.last_seen_at = severity, title, detail[:2000], now
            repeat = timedelta(hours=get_settings().alert_repeat_hours)
            due = escalated or row.last_notified_at is None or now - row.last_notified_at >= repeat
        if due:
            delivered = deliver("CRITICAL" if severity == "critical" else "WARNING", severity, title, detail)
            # An undelivered alert is retried at the next check rather than waiting out the repeat timer.
            if delivered or not (get_settings().alert_webhook_url or get_settings().alert_email_to):
                row.last_notified_at = now
                row.notify_count += 1
        s.commit()
        return due
    finally:
        s.close()


def resolve_alert(key: str) -> bool:
    s = get_session()
    try:
        row = s.query(OpsAlert).filter(OpsAlert.key == key, OpsAlert.resolved_at.is_(None)).first()
        if row is None:
            return False
        row.resolved_at = _now()
        notified = row.notify_count > 0
        title, severity = row.title, row.severity
        s.commit()
        if notified and get_settings().alert_notify_resolved:
            deliver("RESOLVED", severity, title, "")
        return True
    finally:
        s.close()


def sync_alerts(prefix: str, active: dict[str, tuple[str, str, str]]) -> None:
    """Make the open alerts under `prefix` match `active` ({key: (severity, title, detail)}):
    raise what holds, resolve what no longer does."""
    s = get_session()
    try:
        open_keys = [k for (k,) in s.query(OpsAlert.key).filter(OpsAlert.key.like(prefix + "%"), OpsAlert.resolved_at.is_(None))]
    finally:
        s.close()
    for key, (severity, title, detail) in active.items():
        raise_alert(key, severity, title, detail)
    for key in open_keys:
        if key not in active:
            resolve_alert(key)


def open_alerts() -> list[OpsAlert]:
    s = get_session()
    try:
        rows = s.query(OpsAlert).filter(OpsAlert.resolved_at.is_(None)).order_by(OpsAlert.first_seen_at).all()
        s.expunge_all()
        return rows
    finally:
        s.close()
