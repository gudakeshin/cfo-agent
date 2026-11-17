"""Board-ready exports of a digest: PDF (reportlab) and PowerPoint (python-pptx).

Both are built from one payload (`digest_payload`) so the PDF, the deck and the
read-only share page always say the same thing. Every insight carries its sources
as numbered footnotes resolved from the evidence frozen when the digest was made.
"""

from __future__ import annotations

import io
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from cfo_agent.citations import resolve_citation
from cfo_agent.kra import get_active_framework
from cfo_agent.models import Digest, DigestItem, Signal
from cfo_agent.signals import metric_meta

SECTIONS = ("what", "why", "connects", "action")
SECTION_LABEL = {"what": "What happened", "why": "Why it matters", "connects": "How it connects", "action": "Suggested action"}


def _source_label(session: Session, item: DigestItem, cid: str) -> str:
    snap = (item.evidence_snapshot_json or {}).get(cid)
    if isinstance(snap, dict) and snap.get("summary"):
        return str(snap["summary"])
    r = resolve_citation(session, cid)
    return r.summary or cid


def _priority(item: DigestItem) -> str:
    if item.is_red_line:
        return "Red line"
    s = item.priority_score or 0
    return "High" if s >= 2.0 else "Medium" if s >= 1.2 else "Low"


def digest_payload(session: Session, digest: Digest, *, include_internals: bool = False) -> dict[str, Any]:
    fw = get_active_framework(session)
    items = session.query(DigestItem).filter_by(digest_id=digest.id).order_by(DigestItem.rank).all()
    out_items = []
    for it in items:
        cites = it.citations_json or {}
        if isinstance(cites, list):
            cites = {"what": cites}
        ordered: list[str] = []
        for sec in SECTIONS:
            for c in cites.get(sec) or []:
                if c not in ordered:
                    ordered.append(c)
        decomp = it.score_decomp_json or {}
        metric_id = decomp.get("metric_id")
        history = []
        if metric_id:
            rows = (
                session.query(Signal.week, Signal.value)
                .filter(Signal.metric_id == metric_id, Signal.week <= digest.week)
                .order_by(Signal.week.desc())
                .limit(13)
                .all()
            )
            history = [{"week": w, "value": v} for w, v in reversed(rows)]
        entry = {
            "id": it.id,
            "rank": it.rank,
            "title": it.title,
            "sections": {sec: getattr(it, sec) for sec in SECTIONS if getattr(it, sec)},
            "is_red_line": it.is_red_line,
            "priority": _priority(it),
            "kras": [{"id": k, "name": fw.kras[k].name if k in fw.kras else k} for k in (it.kra_ids_json or [])],
            "metric": metric_meta(metric_id) | {"value": decomp.get("value"), "history": history} if metric_id else None,
            "sources": [{"n": i + 1, "id": c, "label": _source_label(session, it, c)} for i, c in enumerate(ordered)],
            "unverified_figures": (it.verification_json or {}).get("unverified_figures") or [],
        }
        if include_internals:
            entry["score_decomp"] = decomp
        out_items.append(entry)
    return {
        "id": digest.id,
        "week": digest.week,
        "headline": digest.headline,
        "weighting_label": digest.weighting_label,
        "created_at": digest.created_at.isoformat() if digest.created_at else None,
        "empty_bar": digest.empty_bar,
        "items": out_items,
        "red_line_count": sum(1 for i in out_items if i["is_red_line"]),
    }


def _fmt_value(meta: dict | None) -> str:
    if not meta or meta.get("value") is None:
        return ""
    v = float(meta["value"])
    unit = meta.get("unit") or ""
    if unit == "₹ Cr":
        return f"₹{v:,.2f} Cr"
    if unit == "%":
        return f"{v:.1f}%"
    return f"{v:,.1f} {unit}".strip()


# ── PDF ─────────────────────────────────────────────────────────────────────

_FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
    "/usr/share/fonts/noto/NotoSans-Regular.ttf",
]
_font_name: str | None = None


def _pdf_font() -> str:
    """A TTF with the ₹ glyph if one is installed; otherwise Helvetica (₹ → Rs.)."""
    global _font_name
    if _font_name:
        return _font_name
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    for path in _FONT_CANDIDATES:
        if Path(path).exists():
            try:
                font = TTFont("CfoSans", path)
                if 0x20B9 not in font.face.charToGlyph:  # must have the ₹ glyph
                    continue
                pdfmetrics.registerFont(font)
                _font_name = "CfoSans"
                return _font_name
            except Exception:  # noqa: BLE001
                continue
    _font_name = "Helvetica"
    return _font_name


def _pdf_text(s: str) -> str:
    from xml.sax.saxutils import escape

    s = escape(s or "")
    return s if _pdf_font() != "Helvetica" else s.replace("₹", "Rs. ")


def _spark_drawing(history: list[dict], width: float = 160, height: float = 36):
    from reportlab.graphics.shapes import Circle, Drawing, PolyLine
    from reportlab.lib import colors

    vals = [h["value"] for h in history if h.get("value") is not None]
    d = Drawing(width, height)
    if len(vals) < 2:
        return d
    lo, hi = min(vals), max(vals)
    span = (hi - lo) or 1.0
    pts = []
    for i, v in enumerate(vals):
        x = 4 + i * (width - 8) / (len(vals) - 1)
        y = 4 + (v - lo) / span * (height - 8)
        pts.extend([x, y])
    d.add(PolyLine(pts, strokeColor=colors.HexColor("#1F4E79"), strokeWidth=1.4))
    d.add(Circle(pts[-2], pts[-1], 2.2, fillColor=colors.HexColor("#1F4E79"), strokeColor=None))
    return d


def render_pdf(payload: dict) -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_LEFT
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    font = _pdf_font()
    base = ParagraphStyle("b", fontName=font, fontSize=9.5, leading=13, alignment=TA_LEFT)
    small = ParagraphStyle("s", parent=base, fontSize=7.5, leading=10, textColor=colors.HexColor("#555555"))
    h1 = ParagraphStyle("h1", parent=base, fontSize=17, leading=21, spaceAfter=4)
    h2 = ParagraphStyle("h2", parent=base, fontSize=12, leading=15, spaceBefore=8, spaceAfter=3)
    label = ParagraphStyle("l", parent=base, fontSize=8, leading=10, textColor=colors.HexColor("#1F4E79"))

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm,
                            topMargin=16 * mm, bottomMargin=16 * mm,
                            title=f"CFO briefing {payload['week']}", author="CFO Intelligence")
    story: list = [
        Paragraph(_pdf_text(f"CFO weekly briefing · {payload['week']}"), small),
        Paragraph(_pdf_text(payload["headline"] or "No headline"), h1),
        Paragraph(_pdf_text(f"Priorities: {payload['weighting_label']} · "
                            f"{len(payload['items'])} insight(s), {payload['red_line_count']} red line(s)"), small),
        Spacer(1, 6),
    ]
    if payload["empty_bar"] or not payload["items"]:
        story.append(Paragraph("Nothing cleared the bar this week.", base))
    for it in payload["items"]:
        block: list = []
        tag = "RED LINE · " if it["is_red_line"] else f"{it['priority'].upper()} · "
        kras = ", ".join(k["name"] for k in it["kras"])
        block.append(Paragraph(_pdf_text(f"{it['rank']}. {it['title']}"), h2))
        block.append(Paragraph(_pdf_text(tag + kras), label))
        if it["metric"]:
            m = it["metric"]
            t = Table([[Paragraph(_pdf_text(m["label"]) + ": <b>" + _pdf_text(_fmt_value(m)) + "</b>", base),
                        _spark_drawing(m["history"])]], colWidths=[100 * mm, 60 * mm])
            t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE")]))
            block.append(t)
        for sec, txt in it["sections"].items():
            block.append(Paragraph(_pdf_text(SECTION_LABEL[sec]), label))
            block.append(Paragraph(_pdf_text(txt), base))
        if it["sources"]:
            src = "; ".join(f"[{s['n']}] {s['label']}" for s in it["sources"][:8])
            more = f" (+{len(it['sources']) - 8} more)" if len(it["sources"]) > 8 else ""
            block.append(Spacer(1, 2))
            block.append(Paragraph(_pdf_text("Sources: " + src + more), small))
        if it["unverified_figures"]:
            block.append(Paragraph(_pdf_text("Not matched to source data: " + ", ".join(it["unverified_figures"])), small))
        story.append(KeepTogether(block))
    story.append(Spacer(1, 10))
    story.append(Paragraph(_pdf_text(
        f"Generated {datetime.utcnow():%d %b %Y %H:%M} UTC. Every figure is computed from the ERP records listed as "
        "sources; insights that could not be cited were withheld."), small))

    def _page(canvas, doc_):
        canvas.saveState()
        canvas.setFont(font, 7)
        canvas.setFillColor(colors.HexColor("#888888"))
        canvas.drawString(18 * mm, 9 * mm, f"Confidential · CFO briefing {payload['week']}")
        canvas.drawRightString(A4[0] - 18 * mm, 9 * mm, f"Page {doc_.page}")
        canvas.restoreState()

    doc.build(story, onFirstPage=_page, onLaterPages=_page)
    return buf.getvalue()


# ── PowerPoint ──────────────────────────────────────────────────────────────

def render_pptx(payload: dict) -> bytes:
    from pptx import Presentation
    from pptx.chart.data import CategoryChartData
    from pptx.dml.color import RGBColor
    from pptx.enum.chart import XL_CHART_TYPE
    from pptx.util import Inches, Pt

    navy, red, grey = RGBColor(0x1F, 0x4E, 0x79), RGBColor(0xB0, 0x24, 0x18), RGBColor(0x59, 0x59, 0x59)
    prs = Presentation()
    prs.slide_width, prs.slide_height = Inches(13.333), Inches(7.5)
    blank = prs.slide_layouts[6]

    def textbox(slide, x, y, w, h, paragraphs, size=14, color=None, bold_first=False):
        tb = slide.shapes.add_textbox(Inches(x), Inches(y), Inches(w), Inches(h))
        tf = tb.text_frame
        tf.word_wrap = True
        for i, text in enumerate(paragraphs):
            p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
            run = p.add_run()
            run.text = text
            run.font.size = Pt(size)
            run.font.bold = bold_first and i == 0
            if color is not None:
                run.font.color.rgb = color
            p.space_after = Pt(6)
        return tb

    # Title slide
    s = prs.slides.add_slide(blank)
    textbox(s, 0.8, 0.6, 11.5, 0.5, [f"CFO weekly briefing · {payload['week']}"], 16, grey)
    textbox(s, 0.8, 1.3, 11.7, 2.4, [payload["headline"] or "No headline"], 32, navy, True)
    textbox(s, 0.8, 4.2, 11.5, 0.8, [f"Priorities: {payload['weighting_label']}",
                                      f"{len(payload['items'])} insight(s) · {payload['red_line_count']} red line(s)"], 16, grey)

    # Summary slide
    s = prs.slides.add_slide(blank)
    textbox(s, 0.8, 0.4, 11.5, 0.8, ["This week at a glance"], 28, navy, True)
    lines = []
    for it in payload["items"]:
        tag = "RED LINE" if it["is_red_line"] else it["priority"]
        val = _fmt_value(it["metric"]) if it["metric"] else ""
        lines.append(f"{it['rank']}. [{tag}] {it['title']}" + (f" — {val}" if val else ""))
    textbox(s, 0.8, 1.4, 11.8, 5.6, lines or ["Nothing cleared the bar this week."], 18)

    # One slide per insight
    for it in payload["items"]:
        s = prs.slides.add_slide(blank)
        tag = "RED LINE" if it["is_red_line"] else f"{it['priority']} priority"
        textbox(s, 0.6, 0.3, 12, 0.4, [f"{tag} · " + ", ".join(k["name"] for k in it["kras"])], 12,
                red if it["is_red_line"] else grey)
        textbox(s, 0.6, 0.7, 12.1, 1.0, [it["title"]], 26, navy, True)
        body = []
        for sec, txt in it["sections"].items():
            body.append(f"{SECTION_LABEL[sec]}: {txt}")
        has_chart = bool(it["metric"] and len(it["metric"]["history"]) >= 2)
        textbox(s, 0.6, 1.8, 7.2 if has_chart else 12.1, 4.6, body, 14)
        if has_chart:
            m = it["metric"]
            cd = CategoryChartData()
            cd.categories = [h["week"].split("-")[-1] for h in m["history"]]
            cd.add_series(m["label"], [h["value"] for h in m["history"]])
            gf = s.shapes.add_chart(XL_CHART_TYPE.LINE_MARKERS, Inches(8.1), Inches(1.9), Inches(4.7), Inches(3.2), cd)
            ch = gf.chart
            ch.has_legend = False
            ch.has_title = True
            ch.chart_title.text_frame.text = f"{m['label']} ({m['unit']})"
            ch.chart_title.text_frame.paragraphs[0].runs[0].font.size = Pt(12)
            ch.series[0].format.line.color.rgb = navy
            ch.value_axis.tick_labels.font.size = Pt(10)
            ch.category_axis.tick_labels.font.size = Pt(10)
            textbox(s, 8.1, 5.15, 4.7, 0.5, [f"Now: {_fmt_value(m)}"], 14, navy, True)
        if it["sources"]:
            src = "; ".join(f"[{x['n']}] {x['label']}" for x in it["sources"][:6])
            more = f" (+{len(it['sources']) - 6} more)" if len(it["sources"]) > 6 else ""
            textbox(s, 0.6, 6.55, 12.1, 0.8, ["Sources: " + src + more], 9, grey)
        notes = s.notes_slide.notes_text_frame
        notes.text = "All sources:\n" + "\n".join(f"[{x['n']}] {x['id']} — {x['label']}" for x in it["sources"])

    s = prs.slides.add_slide(blank)
    textbox(s, 0.8, 0.5, 11.5, 0.8, ["How these insights were produced"], 24, navy, True)
    textbox(s, 0.8, 1.5, 11.5, 4.5, [
        "Metrics are computed deterministically from the ERP records cited on each slide.",
        "Insights are ranked by the CFO office's current priority weighting; red lines always surface.",
        "Every sentence had to cite records that support it; uncited insights were withheld, and figures that "
        "could not be matched to source data are flagged.",
        f"Generated {datetime.utcnow():%d %b %Y %H:%M} UTC.",
    ], 16, grey)

    buf = io.BytesIO()
    prs.save(buf)
    return buf.getvalue()
