"""Digest export (PDF / PowerPoint) and read-only share links."""

from __future__ import annotations

import re
from datetime import datetime, timedelta

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from itsdangerous import BadSignature, URLSafeSerializer
from pydantic import BaseModel, Field

from cfo_agent.db import get_session
from cfo_agent.digest.export import digest_payload, render_pdf, render_pptx
from cfo_agent.models import Digest, ShareLink
from cfo_agent.settings import get_settings

router = APIRouter()

PDF = "application/pdf"
PPTX = "application/vnd.openxmlformats-officedocument.presentationml.presentation"


def _serializer() -> URLSafeSerializer:
    return URLSafeSerializer(get_settings().secret_key or "demo-only-not-secret", salt="cfo-agent/share")


def make_share_token(link_id: int) -> str:
    return _serializer().dumps({"s": link_id})


def _filename(d: Digest, ext: str) -> str:
    return f"cfo-briefing-{re.sub(r'[^0-9A-Za-z-]', '', d.week)}.{ext}"


def _file(content: bytes, media: str, name: str) -> Response:
    return Response(content, media_type=media, headers={
        "Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "no-store",
    })


@router.get("/digests/{digest_id}/export.{fmt}")
def export_digest(digest_id: int, fmt: str):
    if fmt not in ("pdf", "pptx"):
        raise HTTPException(404, "Unknown export format")
    session = get_session()
    try:
        d = session.get(Digest, digest_id)
        if not d:
            raise HTTPException(404, "Digest not found")
        payload = digest_payload(session, d)
        if fmt == "pdf":
            return _file(render_pdf(payload), PDF, _filename(d, "pdf"))
        return _file(render_pptx(payload), PPTX, _filename(d, "pptx"))
    finally:
        session.close()


class ShareBody(BaseModel):
    days: int = Field(7, ge=1, le=30)
    note: str = Field("", max_length=256)


def _link_view(link: ShareLink, request: Request | None = None) -> dict:
    token = make_share_token(link.id)
    s = get_settings()
    # The configured public URL when set (behind a proxy); otherwise the URL this
    # request came in on, so demo / local links work without configuration.
    base = s.public_base_url.rstrip("/") if "public_base_url" in s.model_fields_set else ""
    if not base and request is not None:
        base = str(request.base_url).rstrip("/")
    now = datetime.utcnow()
    return {
        "id": link.id,
        "digest_id": link.digest_id,
        "note": link.note,
        "created_by": link.created_by,
        "created_at": link.created_at,
        "expires_at": link.expires_at,
        "revoked": link.revoked,
        "active": not link.revoked and link.expires_at > now,
        "views": link.views,
        "last_viewed_at": link.last_viewed_at,
        "url": f"{base}/shared/{token}",
    }


@router.post("/digests/{digest_id}/share")
def create_share(digest_id: int, body: ShareBody, request: Request):
    user = getattr(request.state, "user", None)
    session = get_session()
    try:
        if not session.get(Digest, digest_id):
            raise HTTPException(404, "Digest not found")
        link = ShareLink(digest_id=digest_id, created_by=getattr(user, "email", "unknown"), note=body.note,
                         expires_at=datetime.utcnow() + timedelta(days=body.days))
        session.add(link)
        session.commit()
        return _link_view(link, request)
    finally:
        session.close()


@router.get("/digests/{digest_id}/shares")
def list_shares(digest_id: int, request: Request):
    session = get_session()
    try:
        rows = session.query(ShareLink).filter_by(digest_id=digest_id).order_by(ShareLink.id.desc()).all()
        return [_link_view(r, request) for r in rows]
    finally:
        session.close()


@router.post("/shares/{link_id}/revoke")
def revoke_share(link_id: int, request: Request):
    session = get_session()
    try:
        link = session.get(ShareLink, link_id)
        if not link:
            raise HTTPException(404, "Share link not found")
        link.revoked = True
        session.commit()
        return _link_view(link, request)
    finally:
        session.close()


# ── public (token) routes ────────────────────────────────────────────────────

def _open_link(session, token: str) -> tuple[ShareLink, Digest]:
    try:
        data = _serializer().loads(token)
        link = session.get(ShareLink, int(data["s"]))
    except (BadSignature, KeyError, TypeError, ValueError):
        link = None
    # One message for every failure so a link's existence is not revealed.
    if link is None or link.revoked or link.expires_at <= datetime.utcnow():
        raise HTTPException(404, "This link has expired or been revoked")
    d = session.get(Digest, link.digest_id)
    if d is None:
        raise HTTPException(404, "This link has expired or been revoked")
    return link, d


def _record_share_view(request: Request, link: ShareLink) -> None:
    from cfo_agent.audit import record_event

    class _Viewer:
        email = f"share-link:{link.id}"
        role = "external"

    record_event(request, action="view", status=200, user=_Viewer())


@router.get("/shared/{token}")
def view_shared(token: str, request: Request):
    session = get_session()
    try:
        link, d = _open_link(session, token)
        link.views += 1
        link.last_viewed_at = datetime.utcnow()
        session.commit()
        payload = digest_payload(session, d)
        payload["shared"] = {"expires_at": link.expires_at, "shared_by": link.created_by, "note": link.note}
        _record_share_view(request, link)
        return payload
    finally:
        session.close()


@router.get("/shared/{token}/export.pdf")
def export_shared(token: str, request: Request):
    session = get_session()
    try:
        link, d = _open_link(session, token)
        _record_share_view(request, link)
        return _file(render_pdf(digest_payload(session, d)), PDF, _filename(d, "pdf"))
    finally:
        session.close()
