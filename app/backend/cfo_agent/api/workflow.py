"""Action tracker, pre-meeting briefs, feedback-learning proposals, entity view and
industry KRA templates."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from cfo_agent.db import get_session
from cfo_agent.models import ActionItem, DigestItem, MeetingBrief, WeightingProposal

router = APIRouter()

STATUSES = ("open", "in_progress", "done", "dropped")


def _user(request: Request) -> str:
    return getattr(getattr(request.state, "user", None), "email", "unknown")


# ── actions ─────────────────────────────────────────────────────────────────

class ActionBody(BaseModel):
    title: str = Field(..., min_length=3, max_length=512)
    detail: str = Field("", max_length=4000)
    owner: str = Field("", max_length=256)
    due_date: date | None = None
    digest_item_id: int | None = None
    status: str = "open"


class ActionPatch(BaseModel):
    title: str | None = Field(None, min_length=3, max_length=512)
    detail: str | None = Field(None, max_length=4000)
    owner: str | None = Field(None, max_length=256)
    due_date: date | None = None
    clear_due_date: bool = False
    status: str | None = None


def _action_view(a: ActionItem, item: DigestItem | None = None) -> dict:
    today = date.today()
    return {
        "id": a.id, "title": a.title, "detail": a.detail, "owner": a.owner,
        "due_date": a.due_date, "status": a.status, "kra_ids": a.kra_ids_json or [],
        "metric_id": a.metric_id, "digest_item_id": a.digest_item_id,
        "insight_title": item.title if item else None,
        "overdue": bool(a.due_date and a.due_date < today and a.status in ("open", "in_progress")),
        "created_by": a.created_by, "created_at": a.created_at, "updated_at": a.updated_at, "closed_at": a.closed_at,
    }


@router.get("/actions")
def list_actions(status: str | None = None, digest_item_id: int | None = None):
    session = get_session()
    try:
        q = session.query(ActionItem)
        if status == "active":
            q = q.filter(ActionItem.status.in_(("open", "in_progress")))
        elif status in STATUSES:
            q = q.filter(ActionItem.status == status)
        if digest_item_id is not None:
            q = q.filter(ActionItem.digest_item_id == digest_item_id)
        rows = q.order_by(ActionItem.status.in_(("done", "dropped")), ActionItem.due_date.is_(None),
                          ActionItem.due_date, ActionItem.id.desc()).all()
        items = {i.id: i for i in session.query(DigestItem).filter(
            DigestItem.id.in_([r.digest_item_id for r in rows if r.digest_item_id])).all()} if rows else {}
        return [_action_view(r, items.get(r.digest_item_id)) for r in rows]
    finally:
        session.close()


@router.post("/actions")
def create_action(body: ActionBody, request: Request):
    if body.status not in STATUSES:
        raise HTTPException(422, f"status must be one of {', '.join(STATUSES)}")
    session = get_session()
    try:
        item = session.get(DigestItem, body.digest_item_id) if body.digest_item_id else None
        if body.digest_item_id and not item:
            raise HTTPException(404, "Insight not found")
        a = ActionItem(
            title=body.title.strip(), detail=body.detail, owner=body.owner.strip(), due_date=body.due_date,
            status=body.status, digest_item_id=body.digest_item_id, created_by=_user(request),
            kra_ids_json=list(item.kra_ids_json or []) if item else [],
            metric_id=(item.score_decomp_json or {}).get("metric_id") if item else None,
        )
        session.add(a)
        session.commit()
        return _action_view(a, item)
    finally:
        session.close()


@router.put("/actions/{action_id}")
def update_action(action_id: int, body: ActionPatch):
    session = get_session()
    try:
        a = session.get(ActionItem, action_id)
        if not a:
            raise HTTPException(404, "Action not found")
        if body.status is not None:
            if body.status not in STATUSES:
                raise HTTPException(422, f"status must be one of {', '.join(STATUSES)}")
            if body.status in ("done", "dropped") and a.status not in ("done", "dropped"):
                a.closed_at = datetime.utcnow()
            elif body.status in ("open", "in_progress"):
                a.closed_at = None
            a.status = body.status
        for f in ("title", "detail", "owner"):
            v = getattr(body, f)
            if v is not None:
                setattr(a, f, v.strip() if f != "detail" else v)
        if body.due_date is not None:
            a.due_date = body.due_date
        if body.clear_due_date:
            a.due_date = None
        a.updated_at = datetime.utcnow()
        session.commit()
        item = session.get(DigestItem, a.digest_item_id) if a.digest_item_id else None
        return _action_view(a, item)
    finally:
        session.close()


# ── meeting briefs ──────────────────────────────────────────────────────────

class BriefBody(BaseModel):
    title: str = Field(..., min_length=3, max_length=256)
    audience: str = "board"
    meeting_date: date | None = None
    kra_ids: list[str] = Field(default_factory=list)
    notes: str = Field("", max_length=2000)
    week: str | None = None


def _brief_view(b: MeetingBrief) -> dict:
    return {
        "id": b.id, "title": b.title, "audience": b.audience, "meeting_date": b.meeting_date, "week": b.week,
        "request": b.request_json, "content": b.content_json, "verification": b.verification_json,
        "evidence": {k: v.get("summary") for k, v in (b.evidence_snapshot_json or {}).items()},
        "cost_usd": b.cost_usd, "created_by": b.created_by, "created_at": b.created_at,
    }


@router.get("/briefs")
def list_briefs():
    session = get_session()
    try:
        return [
            {"id": b.id, "title": b.title, "audience": b.audience, "meeting_date": b.meeting_date,
             "week": b.week, "created_at": b.created_at, "created_by": b.created_by,
             "fallback": (b.verification_json or {}).get("fallback", False)}
            for b in session.query(MeetingBrief).order_by(MeetingBrief.id.desc()).limit(100)
        ]
    finally:
        session.close()


@router.get("/briefs/audiences")
def brief_audiences():
    from cfo_agent.briefs import AUDIENCES

    return [{"id": k, "description": v} for k, v in AUDIENCES.items()]


@router.get("/briefs/{brief_id}")
def get_brief(brief_id: int):
    session = get_session()
    try:
        b = session.get(MeetingBrief, brief_id)
        if not b:
            raise HTTPException(404, "Brief not found")
        return _brief_view(b)
    finally:
        session.close()


@router.post("/briefs")
def create_brief(body: BriefBody, request: Request):
    from cfo_agent.briefs import AUDIENCES, build_brief
    from cfo_agent.models import Signal

    if body.audience not in AUDIENCES:
        raise HTTPException(422, f"audience must be one of {', '.join(AUDIENCES)}")
    session = get_session()
    try:
        week = body.week or session.query(Signal.week).order_by(Signal.week.desc()).limit(1).scalar()
        if not week:
            raise HTTPException(409, "No signals yet — generate a digest first")
        b = build_brief(session, title=body.title, audience=body.audience, week=week, kra_ids=body.kra_ids,
                        meeting_date=body.meeting_date, notes=body.notes, user=_user(request))
        session.commit()
        return _brief_view(b)
    finally:
        session.close()


# ── feedback learning ───────────────────────────────────────────────────────

def _proposal_view(session, p: WeightingProposal) -> dict:
    from cfo_agent.kra import get_active_framework

    fw = get_active_framework(session)
    return {
        "id": p.id, "status": p.status, "created_at": p.created_at, "base_weighting_id": p.base_weighting_id,
        "changes": [
            {"kra_id": k, "kra_name": fw.kras[k].name if k in fw.kras else k, "to": v,
             "from": (p.evidence_json.get(k) or {}).get("from", 1.0), "evidence": p.evidence_json.get(k)}
            for k, v in sorted((p.multipliers_json or {}).items())
        ],
        "decided_by": p.decided_by, "decided_at": p.decided_at, "applied_weighting_id": p.applied_weighting_id,
    }


@router.get("/learning/proposals")
def list_proposals():
    from cfo_agent.learning import CEILING, FLOOR, MAX_STEP, MIN_SAMPLES, feedback_evidence

    session = get_session()
    try:
        rows = session.query(WeightingProposal).order_by(WeightingProposal.id.desc()).limit(20).all()
        return {
            "proposals": [_proposal_view(session, p) for p in rows],
            "evidence": feedback_evidence(session),
            "guardrails": {"min_samples": MIN_SAMPLES, "floor": FLOOR, "ceiling": CEILING, "max_step": MAX_STEP},
        }
    finally:
        session.close()


@router.post("/learning/propose")
def make_proposal():
    from cfo_agent.learning import propose

    session = get_session()
    try:
        p = propose(session)
        session.commit()
        return {"proposal": _proposal_view(session, p) if p else None,
                "message": None if p else "Not enough feedback yet to justify a change"}
    finally:
        session.close()


@router.post("/learning/proposals/{pid}/{decision}")
def decide_proposal(pid: int, decision: str, request: Request):
    from cfo_agent.learning import approve, reject

    if decision not in ("approve", "reject"):
        raise HTTPException(404)
    session = get_session()
    try:
        p = session.get(WeightingProposal, pid)
        if not p:
            raise HTTPException(404, "Proposal not found")
        try:
            if decision == "approve":
                approve(session, p, user=_user(request))
            else:
                reject(session, p, user=_user(request))
        except ValueError as e:
            raise HTTPException(409, str(e)) from None
        session.commit()
        return _proposal_view(session, p)
    finally:
        session.close()


# ── entities ────────────────────────────────────────────────────────────────

ENTITY_METRICS = ["cash_balance_cr", "dso_days", "ar_overdue_pct", "ap_overdue_pct", "free_cash_flow_cr"]


@router.get("/insights/entities")
def entity_breakdown(week: str | None = None):
    """Key metrics per entity plus the consolidated group view."""
    from cfo_agent.api.insights import _data_weeks
    from cfo_agent.models import ErpEntity
    from cfo_agent.signals import COMPUTE_FNS, entity_scope, metric_meta

    session = get_session()
    try:
        weeks = _data_weeks(session, 1)
        week = week or (weeks[-1] if weeks else None)
        if not week:
            return {"week": None, "entities": [], "metrics": []}
        ents = session.query(ErpEntity).order_by(ErpEntity.business_key).all() or []

        def row(scope: str | None) -> dict[str, Any]:
            with entity_scope(scope):
                vals = {}
                for m in ENTITY_METRICS:
                    r = COMPUTE_FNS[m](session, week)
                    vals[m] = r.value if r.available and r.lineage else None
                return vals

        out = [{"id": e.business_key, "name": e.name, "currency": e.functional_currency,
                "consolidated": e.consolidate, "values": row(e.business_key)} for e in ents]
        return {"week": week, "metrics": [metric_meta(m) for m in ENTITY_METRICS],
                "group": row(None), "entities": out}
    finally:
        session.close()


# ── industry templates ──────────────────────────────────────────────────────

@router.get("/kra/templates")
def kra_templates():
    from cfo_agent.templates import list_templates

    return list_templates()


@router.get("/kra/templates/{template_id}")
def kra_template(template_id: str):
    from cfo_agent.templates import load_template

    try:
        return load_template(template_id)
    except KeyError:
        raise HTTPException(404, "Template not found") from None
