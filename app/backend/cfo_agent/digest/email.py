"""Email delivery: Resend → SMTP → file outbox. Mobile-first HTML with a plain-text
alternative and links back into the app."""

from __future__ import annotations

import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy.orm import Session

from cfo_agent.models import Alert, Digest, DigestItem
from cfo_agent.settings import OUTBOX_DIR, get_settings

logger = logging.getLogger(__name__)

TEMPLATE_DIR = Path(__file__).parent / "templates"
# Templates are named *.html.j2 / *.txt.j2: escape the HTML ones explicitly (the
# default only escapes names ending in ".html", which would let AI-written text
# inject markup into the email).
env = Environment(
    loader=FileSystemLoader(str(TEMPLATE_DIR)),
    autoescape=select_autoescape(enabled_extensions=("html.j2", "xml.j2"), default=False),
    trim_blocks=True,
    lstrip_blocks=True,
)


def app_base_url() -> str:
    """Where the app lives for the current client — the target of every link in an
    email. In multi-tenant mode that is the tenant's own host, never another's."""
    from cfo_agent.tenancy import current_tenant

    base = get_settings().public_base_url.rstrip("/")
    tenant = current_tenant()
    if tenant and tenant.hosts:
        scheme = base.split("://", 1)[0] if "://" in base else "https"
        return f"{scheme}://{tenant.hosts[0]}"
    return base


def week_label(week: str) -> str:
    """'2026-W28' -> 'Week 28 (6–12 Jul)', as the app shows it."""
    from cfo_agent.timeutil import week_monday, week_sunday

    mon, sun = week_monday(week), week_sunday(week)
    number = week.split("-W")[1].lstrip("0") or "0"
    if mon.month == sun.month:
        span = f"{mon.day}–{sun.day} {sun:%b}"
    else:
        span = f"{mon.day} {mon:%b} – {sun.day} {sun:%b}"
    return f"Week {number} ({span})"


def priority_label(score: float, is_red_line: bool) -> str:
    """Same bars as the app's priority tag (and the pipeline's risk-appetite thresholds)."""
    if is_red_line:
        return "Red line"
    if score >= 2:
        return "High priority"
    if score >= 1.2:
        return "Medium priority"
    return "Watch"


def _first_sentences(text: str, n: int = 3) -> str:
    """The opening of a section; a phone email is a summary, the app has the rest."""
    import re

    parts = re.split(r"(?<=[.!?])\s+", (text or "").strip())
    return " ".join(parts[:n])


def _email_item(item: DigestItem, base: str, digest_id: int) -> dict:
    from cfo_agent.timeutil import format_inr

    decomp = item.score_decomp_json or {}
    cont = decomp.get("continuity") or {}
    if cont.get("status") == "continuing":
        status = f"Continuing since {str(cont.get('since_week', '')).split('-')[-1]}"
    elif cont.get("status") == "new" and not cont.get("first_briefing"):
        status = "New this week"
    else:
        status = ""
    movers = [
        f"{r['label']} {format_inr(r['current_paise'])}"
        for r in ((decomp.get("drivers") or {}).get("rows") or [])[:2]
    ]
    return {
        "rank": item.rank,
        "title": item.title,
        "what": _first_sentences(item.what),
        "action": _first_sentences(item.action, 2),
        "is_red_line": item.is_red_line,
        "priority": priority_label(item.priority_score, item.is_red_line),
        "status": status,
        "movers": movers,
        "url": f"{base}/digests/{digest_id}#item-{item.id}" if base else "",
    }


def digest_email_context(session: Session, digest: Digest) -> dict:
    base = app_base_url()
    items = (
        session.query(DigestItem).filter_by(digest_id=digest.id).order_by(DigestItem.rank).all()
    )
    rendered = [_email_item(it, base, digest.id) for it in items]
    reds = sum(1 for it in rendered if it["is_red_line"])
    if digest.empty_bar or not rendered:
        preheader = "Nothing met the surfacing bar this week."
    else:
        preheader = f"{len(rendered)} item{'s' if len(rendered) != 1 else ''}"
        preheader += f", {reds} red line{'s' if reds != 1 else ''}. " if reds else ". "
        preheader += digest.headline
    return {
        "digest": digest,
        "week_label": week_label(digest.week),
        "items": rendered,
        "red_lines": reds,
        "preheader": preheader[:140],
        "data_health": digest.data_health_json or {},
        "open_url": f"{base}/digests/{digest.id}" if base else "",
        "ask_url": f"{base}/ask?digest={digest.id}" if base else "",
    }


def render_digest_html(session: Session, digest: Digest) -> str:
    return env.get_template("email_digest.html.j2").render(**digest_email_context(session, digest))


def render_digest_text(session: Session, digest: Digest) -> str:
    return env.get_template("email_digest.txt.j2").render(**digest_email_context(session, digest))


def alert_email_context(alert: Alert) -> dict:
    from cfo_agent.pipeline.fallback import _val
    from cfo_agent.timeutil import format_inr

    base = app_base_url()
    ev = alert.evidence_json or {}
    metric = ev.get("metric")
    facts: list[tuple[str, str]] = []
    if metric and ev.get("value") is not None:
        from cfo_agent.signals import metric_meta

        facts.append((metric_meta(metric)["label"], _val(metric, ev["value"])))
        if ev.get("threshold") is not None:
            facts.append(("Red-line threshold", _val(metric, ev["threshold"])))
    events = [
        {"label": e.get("business_key", ""), "amount": format_inr(e["amount_paise"]) if e.get("amount_paise") is not None else ""}
        for e in (ev.get("events") or [])[:5]
    ]
    narrative = (alert.synthesis_json or {}).get("narrative", "")
    return {
        "alert": alert,
        "week_label": week_label(alert.week),
        "facts": facts,
        "events": events,
        "narrative": narrative,
        "open_url": f"{base}/" if base else "",
    }


def render_alert_html(session: Session, alert: Alert) -> str:
    return env.get_template("alert_email.html.j2").render(**alert_email_context(alert))


def render_alert_text(session: Session, alert: Alert) -> str:
    return env.get_template("alert_email.txt.j2").render(**alert_email_context(alert))


def _recipients() -> list[str]:
    """Who receives mail for the current tenant. In multi-tenant mode only the
    tenant's own `email_to` is used — never the global EMAIL_TO, which would send
    one client's figures to another's address. No recipients → outbox only."""
    from cfo_agent.tenancy import current_tenant, is_multi_tenant, parse_recipients

    if is_multi_tenant():
        tenant = current_tenant()
        return list(tenant.email_to) if tenant else []
    return list(parse_recipients(get_settings().email_to))


def _sender() -> str:
    from cfo_agent.tenancy import current_tenant

    tenant = current_tenant()
    return (tenant.email_from if tenant and tenant.email_from else None) or get_settings().email_from


def outbox_dir() -> Path:
    """Per-tenant folder, so tenants' files can neither collide nor mix."""
    from cfo_agent.tenancy import current_tenant_id, is_multi_tenant

    if is_multi_tenant():
        return OUTBOX_DIR / (current_tenant_id() or "_unrouted")
    return OUTBOX_DIR


def _write_outbox(name: str, html: str) -> Path:
    folder = outbox_dir()
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_text(html, encoding="utf-8")
    return path


def _send_resend(html: str, subject: str, to: list[str], text: str | None = None) -> bool:
    settings = get_settings()
    if not settings.resend_api_key:
        return False
    import httpx

    resp = httpx.post(
        "https://api.resend.com/emails",
        headers={"Authorization": f"Bearer {settings.resend_api_key}"},
        json={
            "from": _sender(),
            "to": to,
            "subject": subject,
            "html": html,
            **({"text": text} if text else {}),
        },
        timeout=30,
    )
    resp.raise_for_status()
    return True


def _send_smtp(html: str, subject: str, to: list[str], text: str | None = None) -> bool:
    settings = get_settings()
    if not settings.smtp_host:
        return False
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    sender = _sender()
    msg["From"] = sender
    msg["To"] = ", ".join(to)
    if text:  # plain text first: clients show the last part they understand
        msg.attach(MIMEText(text, "plain", "utf-8"))
    msg.attach(MIMEText(html, "html", "utf-8"))
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port) as server:
        server.starttls()
        if settings.smtp_user:
            server.login(settings.smtp_user, settings.smtp_password or "")
        server.sendmail(sender, to, msg.as_string())
    return True


def deliver_html(html: str, subject: str, outbox_name: str, text: str | None = None) -> tuple[str, str | None]:
    """Returns (status, path). status: sent|outbox|failed."""
    to = _recipients()
    if not to:
        logger.warning("No email recipients configured for this tenant; writing to outbox only")
    else:
        try:
            if _send_resend(html, subject, to, text):
                return "sent", None
            if _send_smtp(html, subject, to, text):
                return "sent", None
        except Exception as e:  # noqa: BLE001
            logger.warning("Email send failed, falling back to outbox: %s", e)
    path = _write_outbox(outbox_name, html)
    return "outbox", str(path)


def send_digest_email(session: Session, digest: Digest) -> Digest:
    html = render_digest_html(session, digest)
    status, path = deliver_html(
        html,
        subject=f"CFO briefing · {week_label(digest.week).split(' (')[0]}: {digest.headline[:90]}",
        outbox_name=f"digest-{digest.week}-{digest.id}.html",
        text=render_digest_text(session, digest),
    )
    digest.email_status = status
    digest.email_path = path
    session.flush()
    return digest


def send_alert_email(session: Session, alert: Alert) -> Alert:
    html = render_alert_html(session, alert)
    status, path = deliver_html(
        html,
        subject=f"RED LINE: {alert.title}",
        outbox_name=f"alert-{alert.week}-{alert.id}.html",
        text=render_alert_text(session, alert),
    )
    alert.email_status = status
    alert.email_path = path
    session.flush()
    return alert
