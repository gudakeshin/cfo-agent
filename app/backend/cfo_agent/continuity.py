"""Is this finding new, or carried over from earlier briefings?

The digest text used to contradict itself across weeks ("a fresh matter ... the same
notice first flagged in W27") because nothing told the model — or checked it — which
it was. Continuity is computed here from the stored digests, handed to the model as
a fact, and the finished text is checked against it.

Identity of a finding across weeks matches the Briefing's "what changed" panel: its
metric, else its red-line rule.
"""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy.orm import Session

from cfo_agent.models import Digest, DigestItem

MAX_LOOKBACK = 8


def key_for_filter_item(item: Any) -> str:
    """Same identity as api.insights.item_key, for an item not yet stored."""
    for sid in item.signal_ids or []:
        parts = str(sid).split(":")
        if len(parts) >= 2:
            return f"metric:{parts[1]}"
    if item.rule_fire is not None:
        return f"redline:{item.rule_fire.rule_id}"
    return f"title:{(item.score_decomp or {}).get('title', '')}"


def key_for_stored_item(item: DigestItem) -> str:
    for sid in item.signal_ids_json or []:
        parts = str(sid).split(":")
        if len(parts) >= 2:
            return f"metric:{parts[1]}"
    rl = (item.score_decomp_json or {}).get("red_line")
    if rl:
        return f"redline:{rl}"
    return f"title:{item.title.lower()}"


def _prior_digests(session: Session, week: str) -> list[Digest]:
    """Latest digest of each earlier week, most recent first."""
    seen: dict[str, Digest] = {}
    for d in (
        session.query(Digest).filter(Digest.week < week)
        .order_by(Digest.week.desc(), Digest.created_at.desc(), Digest.id.desc())
    ):
        seen.setdefault(d.week, d)
        if len(seen) >= MAX_LOOKBACK:
            break
    return list(seen.values())


def continuity_for(session: Session, key: str, week: str) -> dict:
    """{"status": "new" | "continuing", "last_week", "since_week", "weeks_running"}.
    `weeks_running` counts consecutive earlier briefings that carried the finding
    (the current one is not counted); it is capped by MAX_LOOKBACK."""
    prior = _prior_digests(session, week)
    if not prior:
        return {"status": "new", "first_briefing": True}
    run: list[str] = []
    for d in prior:  # most recent first; stop at the first briefing without it
        if any(key_for_stored_item(i) == key for i in d.items):
            run.append(d.week)
        else:
            break
    if not run:
        return {"status": "new"}
    return {"status": "continuing", "last_week": run[0], "since_week": run[-1], "weeks_running": len(run)}


# ── checking the written text ────────────────────────────────────────────────

# Wording that claims the finding appeared for the first time.
_CLAIMS_NEW = re.compile(
    r"\b(fresh|newly|brand[- ]new|new (this week|event|matter|breach|notice|issue|development|trip)|"
    r"first (time|flagged|raised|seen|appeared|trip|breach)|(just|only) (appeared|emerged|surfaced|tripped)|"
    r"has (just )?(emerged|appeared) this week)\b",
    re.IGNORECASE,
)
# Wording that claims it has been seen before.
_CLAIMS_CARRIED = re.compile(
    r"\b((flagged|raised|reported|tripped|breached|triggered|surfaced|appears?|appeared) again|"
    r"again (flagged|raised|reported|tripped|breached|triggered)|recurring (notice|issue|breach|matter|event)|"
    r"carried (forward|over)|same (notice|issue|breach|matter|event|item)|"
    r"(first )?(flagged|raised|reported|logged|tripped|surfaced) (in|earlier|previously|last|before)|"
    r"previously (flagged|reported|raised|surfaced|seen)|already (flagged|reported|raised)|"
    r"(unchanged|unresolved) (since|from))\b",
    re.IGNORECASE,
)


# "not a fresh matter", "rather than newly raised": the phrase is being denied, not claimed.
_NEGATED = re.compile(
    r"(\bnot|n't|\bno|\bnever|\bnor|rather than|instead of|as opposed to)\s+"
    r"(?:(?:a|an|the|just|merely|simply|really|truly|yet|quite)\s+)*$",
    re.IGNORECASE,
)


def _claims(pattern: re.Pattern, text: str):
    for m in pattern.finditer(text):
        if not _NEGATED.search(text[max(0, m.start() - 40):m.start()]):
            yield m


def _strip(text: str, ignore: list[str]) -> str:
    out = text or ""
    for phrase in ignore:
        if phrase:
            out = re.sub(re.escape(phrase), " ", out, flags=re.IGNORECASE)
    return out


def continuity_conflicts(texts: list[str], continuity: dict | None, ignore: list[str] | None = None) -> list[str]:
    """Plain-language conflicts between what the text claims and what the digests
    show. `ignore` removes red-line descriptions first: a rule called 'Any new GST
    show-cause notice' describes the rule, not this week's finding."""
    if not continuity:
        return []
    joined = " ".join(_strip(t, ignore or []) for t in texts)
    found: list[str] = []
    if continuity["status"] == "continuing":
        for m in _claims(_CLAIMS_NEW, joined):
            found.append(
                f"The text says “{m.group(0)}”, but this finding was already in the "
                f"{continuity['last_week']} briefing."
            )
    else:
        for m in _claims(_CLAIMS_CARRIED, joined):
            found.append(
                f"The text says “{m.group(0)}”, but this finding was not in any earlier briefing."
            )
    return list(dict.fromkeys(found))[:3]


def continuity_prompt(continuity: dict) -> str:
    if continuity["status"] == "continuing":
        n = continuity["weeks_running"]
        return (
            f"This finding is CONTINUING: it was in the {continuity['last_week']} briefing and has appeared in "
            f"{n} consecutive earlier briefing{'s' if n != 1 else ''} (since {continuity['since_week']}). "
            "Describe it as continuing; never call it new, fresh or newly raised."
        )
    return (
        "This finding is NEW: it was not in any earlier briefing. Do not say it was flagged, reported or seen "
        "before, and do not say 'again', 'still' or 'continues' about the finding itself."
    )
