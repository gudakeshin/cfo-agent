"""Email delivery: Resend → SMTP → file outbox."""

from __future__ import annotations

import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy.orm import Session

from cfo_agent.citations import resolve_citation
from cfo_agent.models import Alert, Digest, DigestItem
from cfo_agent.settings import OUTBOX_DIR, get_settings

logger = logging.getLogger(__name__)

TEMPLATE_DIR = Path(__file__).parent / "templates"
env = Environment(
    loader=FileSystemLoader(str(TEMPLATE_DIR)),
    autoescape=select_autoescape(["html", "xml"]),
)


def _footnote_citations(session: Session, item: DigestItem) -> list[str]:
    notes = []
    all_cites = []
    for lst in (item.citations_json or {}).values():
        all_cites.extend(lst)
    for cid in dict.fromkeys(all_cites):
        r = resolve_citation(session, cid)
        if r.ok:
            notes.append(f"{r.summary} [{cid}]")
        else:
            notes.append(f"{cid}")
    return notes


def render_digest_html(session: Session, digest: Digest) -> str:
    items = (
        session.query(DigestItem)
        .filter_by(digest_id=digest.id)
        .order_by(DigestItem.rank)
        .all()
    )
    rendered = []
    for it in items:
        rendered.append(
            {
                "rank": it.rank,
                "title": it.title,
                "what": it.what,
                "why": it.why,
                "connects": it.connects,
                "action": it.action,
                "kra_ids": it.kra_ids_json,
                "is_red_line": it.is_red_line,
                "footnotes": _footnote_citations(session, it),
            }
        )
    tmpl = env.get_template("email_digest.html.j2")
    return tmpl.render(
        digest=digest,
        items=rendered,
        data_health=digest.data_health_json or {},
    )


def render_alert_html(session: Session, alert: Alert) -> str:
    tmpl = env.get_template("alert_email.html.j2")
    return tmpl.render(alert=alert)


def _write_outbox(name: str, html: str) -> Path:
    OUTBOX_DIR.mkdir(parents=True, exist_ok=True)
    path = OUTBOX_DIR / name
    path.write_text(html, encoding="utf-8")
    return path


def _send_resend(html: str, subject: str) -> bool:
    settings = get_settings()
    if not settings.resend_api_key:
        return False
    import httpx

    resp = httpx.post(
        "https://api.resend.com/emails",
        headers={"Authorization": f"Bearer {settings.resend_api_key}"},
        json={
            "from": settings.email_from,
            "to": [settings.email_to],
            "subject": subject,
            "html": html,
        },
        timeout=30,
    )
    resp.raise_for_status()
    return True


def _send_smtp(html: str, subject: str) -> bool:
    settings = get_settings()
    if not settings.smtp_host:
        return False
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = settings.email_from
    msg["To"] = settings.email_to
    msg.attach(MIMEText(html, "html"))
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port) as server:
        server.starttls()
        if settings.smtp_user:
            server.login(settings.smtp_user, settings.smtp_password or "")
        server.sendmail(settings.email_from, [settings.email_to], msg.as_string())
    return True


def deliver_html(html: str, subject: str, outbox_name: str) -> tuple[str, str | None]:
    """Returns (status, path). status: sent|outbox|failed."""
    try:
        if _send_resend(html, subject):
            return "sent", None
        if _send_smtp(html, subject):
            return "sent", None
    except Exception as e:  # noqa: BLE001
        logger.warning("Email send failed, falling back to outbox: %s", e)
    path = _write_outbox(outbox_name, html)
    return "outbox", str(path)


def send_digest_email(session: Session, digest: Digest) -> Digest:
    html = render_digest_html(session, digest)
    status, path = deliver_html(
        html,
        subject=f"CFO Digest {digest.week}: {digest.headline[:80]}",
        outbox_name=f"digest-{digest.week}-{digest.id}.html",
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
    )
    alert.email_status = status
    alert.email_path = path
    session.flush()
    return alert
