"""Declarative metrics: new KRA metrics defined as data, not code.

A definition names some *inputs* (aggregates over whitelisted ERP tables) and a
*formula* over them:

    id: gross_margin_pct
    label: Gross margin (month to date)
    unit: "%"
    adverse_direction: down          # up | down | null (both)
    inputs:
      revenue: {agg: sum, source: gl_entries, where: {account_type: revenue}, window: month_to_date, sign: -1}
      cogs:    {agg: sum, source: gl_entries, where: {account_type: cogs},    window: month_to_date}
    formula: "100 * (revenue - cogs) / revenue"

Aggregates: sum | avg | count | latest (sum/avg/latest of money columns are ₹ Cr),
or `metric` to reuse another metric's value. Windows: week, as_of, month_to_date,
last_month, quarter_to_date, trailing_<n>d. Formulas allow + - * / ( ), numbers, input names and
min/max/abs — evaluated with a whitelisting AST walker, never eval(). A zero
denominator makes the metric unavailable for that week (no signal, no red line).

Definitions are versioned rows in `metric_definitions`; config/metrics.yaml seeds the
table once. Every process checks a cheap stamp before computing signals, so a metric
saved in the UI is live in the API and the worker without a restart.
"""

from __future__ import annotations

import ast
import logging
import operator
import re
import threading
from datetime import date, timedelta
from typing import Any

import yaml
from sqlalchemy import func
from sqlalchemy.orm import Session

from cfo_agent.models import (
    ErpCreditNote,
    ErpExpense,
    ErpGlEntry,
    ErpGstEvent,
    ErpPayment,
    ErpPurchaseInvoice,
    ErpReceipt,
    ErpRevenueDaily,
    ErpSalesInvoice,
    MetricDefinition,
)
from cfo_agent.settings import CONFIG_DIR
from cfo_agent.timeutil import week_monday, week_sunday

logger = logging.getLogger(__name__)

# source -> (model, date column, money column or None, filterable columns)
SOURCES: dict[str, tuple[type, str, str | None, set[str]]] = {
    "gl_entries": (ErpGlEntry, "posting_date", "amount_paise", {"account_type", "account_code", "entity"}),
    "sales_invoices": (ErpSalesInvoice, "invoice_date", "amount_paise", {"segment", "status", "entity", "currency"}),
    "receipts": (ErpReceipt, "receipt_date", "amount_paise", {"currency"}),
    "credit_notes": (ErpCreditNote, "note_date", "amount_paise", {"reason", "currency"}),
    "purchase_invoices": (ErpPurchaseInvoice, "invoice_date", "amount_paise", {"vendor_name", "status", "entity"}),
    "payments": (ErpPayment, "payment_date", "amount_paise", {"currency"}),
    "expenses": (ErpExpense, "expense_date", "amount_paise", {"cost_center", "category", "entity"}),
    "revenue_daily": (ErpRevenueDaily, "as_of_date", "amount_paise", {"segment", "entity"}),
    "gst_events": (ErpGstEvent, "event_date", "amount_paise", {"event_type", "status", "entity"}),
}
AGGS = {"sum", "avg", "count", "latest", "metric"}
UNITS = {"%", "₹ Cr", "days", "count", "score", "x", ""}
ID_RE = re.compile(r"^[a-z][a-z0-9_]{2,63}$")
WINDOW_RE = re.compile(r"^(week|as_of|month_to_date|last_month|quarter_to_date|trailing_(\d{1,3})d)$")

_BIN = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv}
_FUNCS = {"min": min, "max": max, "abs": abs}


class MetricDefinitionError(ValueError):
    pass


class _Unavailable(Exception):
    pass


# ── formula ─────────────────────────────────────────────────────────────────

def parse_formula(formula: str, names: set[str]) -> ast.Expression:
    try:
        tree = ast.parse(str(formula), mode="eval")
    except SyntaxError as e:
        raise MetricDefinitionError(f"formula: {e.msg}") from None
    for node in ast.walk(tree):
        if isinstance(node, (ast.Expression, ast.Load, ast.operator, ast.unaryop)):
            continue
        if isinstance(node, ast.BinOp) and type(node.op) in _BIN:
            continue
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
            continue
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
            continue
        if isinstance(node, ast.Name):
            if node.id not in names and node.id not in _FUNCS:
                raise MetricDefinitionError(f"formula uses unknown input '{node.id}'")
            continue
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _FUNCS and not node.keywords:
            continue
        raise MetricDefinitionError(f"formula: '{ast.unparse(node)}' is not allowed")
    return tree


def _eval(node: ast.AST, env: dict[str, float]) -> float:
    if isinstance(node, ast.Expression):
        return _eval(node.body, env)
    if isinstance(node, ast.Constant):
        return float(node.value)
    if isinstance(node, ast.Name):
        return env[node.id]
    if isinstance(node, ast.UnaryOp):
        v = _eval(node.operand, env)
        return -v if isinstance(node.op, ast.USub) else v
    if isinstance(node, ast.BinOp):
        a, b = _eval(node.left, env), _eval(node.right, env)
        if isinstance(node.op, ast.Div) and abs(b) < 1e-12:
            raise _Unavailable("division by zero")
        return _BIN[type(node.op)](a, b)
    if isinstance(node, ast.Call):
        return float(_FUNCS[node.func.id](*[_eval(a, env) for a in node.args]))
    raise _Unavailable("bad node")


# ── validation ──────────────────────────────────────────────────────────────

def validate_definition(d: dict[str, Any], *, builtin_ids: set[str]) -> dict[str, Any]:
    """Normalise and validate; raises MetricDefinitionError with a user-facing message."""
    if not isinstance(d, dict):
        raise MetricDefinitionError("definition must be an object")
    mid = str(d.get("id") or "").strip()
    if not ID_RE.match(mid):
        raise MetricDefinitionError("id must be 3–64 chars: lowercase letters, digits, underscores")
    if mid in builtin_ids:
        raise MetricDefinitionError(f"'{mid}' is a built-in metric; choose another id")
    label = str(d.get("label") or "").strip()
    if not label:
        raise MetricDefinitionError("label is required")
    unit = str(d.get("unit") or "")
    if unit not in UNITS:
        raise MetricDefinitionError(f"unit must be one of {', '.join(repr(u) for u in sorted(UNITS))}")
    direction = d.get("adverse_direction")
    if direction not in ("up", "down", None):
        raise MetricDefinitionError("adverse_direction must be up, down or null")
    inputs = d.get("inputs") or {}
    if not isinstance(inputs, dict) or not inputs:
        raise MetricDefinitionError("at least one input is required")
    clean_inputs: dict[str, dict] = {}
    for name, spec in inputs.items():
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,31}", str(name)) or name in _FUNCS:
            raise MetricDefinitionError(f"input name '{name}' is not valid")
        if not isinstance(spec, dict):
            raise MetricDefinitionError(f"input '{name}' must be an object")
        agg = spec.get("agg", "sum")
        if agg not in AGGS:
            raise MetricDefinitionError(f"input '{name}': agg must be one of {', '.join(sorted(AGGS))}")
        out: dict[str, Any] = {"agg": agg}
        if agg == "metric":
            ref = str(spec.get("metric") or "")
            if not ref or ref == mid:
                raise MetricDefinitionError(f"input '{name}': metric must name another metric")
            out["metric"] = ref
        else:
            src = spec.get("source")
            if src not in SOURCES:
                raise MetricDefinitionError(f"input '{name}': source must be one of {', '.join(sorted(SOURCES))}")
            out["source"] = src
            where = spec.get("where") or {}
            allowed = SOURCES[src][3]
            for col, val in where.items():
                if col not in allowed:
                    raise MetricDefinitionError(f"input '{name}': cannot filter {src} by '{col}' (allowed: {', '.join(sorted(allowed))})")
                if not isinstance(val, (str, int, float, list)):
                    raise MetricDefinitionError(f"input '{name}': filter values must be text, numbers or a list")
            out["where"] = dict(where)
            window = str(spec.get("window", "week"))
            if not WINDOW_RE.match(window):
                raise MetricDefinitionError(f"input '{name}': window must be week, as_of, month_to_date, last_month, quarter_to_date or trailing_<n>d")
            out["window"] = window
            sign = spec.get("sign", 1)
            if sign not in (1, -1):
                raise MetricDefinitionError(f"input '{name}': sign must be 1 or -1")
            out["sign"] = sign
        clean_inputs[str(name)] = out
    formula = str(d.get("formula") or "").strip()
    if not formula:
        raise MetricDefinitionError("formula is required")
    parse_formula(formula, set(clean_inputs))
    return {
        "id": mid,
        "label": label,
        "unit": unit,
        "adverse_direction": direction,
        "description": str(d.get("description") or "")[:500],
        "inputs": clean_inputs,
        "formula": formula,
    }


# ── evaluation ──────────────────────────────────────────────────────────────

def _window(window: str, week: str) -> tuple[date | None, date]:
    end = week_sunday(week)
    if window == "week":
        return week_monday(week), end
    if window == "as_of":
        return None, end
    if window == "month_to_date":
        return end.replace(day=1), end
    if window == "last_month":  # the previous full calendar month (a closed period)
        last_end = end.replace(day=1) - timedelta(days=1)
        return last_end.replace(day=1), last_end
    if window == "quarter_to_date":
        q_month = 3 * ((end.month - 1) // 3) + 1
        return end.replace(month=q_month, day=1), end
    m = WINDOW_RE.match(window)
    days = int(m.group(2))
    return end - timedelta(days=days - 1), end


def _input_value(session: Session, spec: dict, week: str, lineage: list[tuple[int, str]]) -> float:
    from cfo_agent.signals import COMPUTE_FNS, _entity_conds

    if spec["agg"] == "metric":
        fn = COMPUTE_FNS.get(spec["metric"])
        if fn is None:
            raise _Unavailable(f"unknown metric {spec['metric']}")
        r = fn(session, week)
        if not r.available:
            raise _Unavailable(f"{spec['metric']} unavailable")
        lineage.extend((0, c) for c in r.lineage[:10])
        return float(r.value)
    model, date_col, money_col, _ = SOURCES[spec["source"]]
    dcol = getattr(model, date_col)
    start, end = _window(spec["window"], week)
    conds = [dcol <= end] + ([dcol >= start] if start else []) + _entity_conds(session, model)
    for col, val in (spec.get("where") or {}).items():
        c = getattr(model, col)
        conds.append(c.in_(val) if isinstance(val, list) else c == val)
    q = session.query(model).filter(*conds)
    agg = spec["agg"]
    if agg == "count":
        value = float(session.query(func.count(model.id)).filter(*conds).scalar() or 0)
    elif agg == "latest":
        row = q.order_by(dcol.desc(), model.id.desc()).first()
        if row is None:
            raise _Unavailable("no rows")
        lineage.append((abs(getattr(row, money_col)), f"erp:{model.__tablename__}:{row.business_key}"))
        return spec["sign"] * getattr(row, money_col) / 1e9
    else:
        col = getattr(model, money_col)
        raw = session.query(func.sum(col) if agg == "sum" else func.avg(col)).filter(*conds).scalar()
        value = float(raw or 0) / 1e9  # paise → ₹ Cr
    top = q.order_by(func.abs(getattr(model, money_col)).desc()).limit(15).all() if money_col else q.limit(15).all()
    lineage.extend((abs(getattr(r, money_col) or 0) if money_col else 0, f"erp:{model.__tablename__}:{r.business_key}") for r in top)
    return spec.get("sign", 1) * value


def evaluate(session: Session, definition: dict, week: str):
    """Compute a declarative metric for `week` → signals.MetricResult."""
    from cfo_agent.signals import MetricResult, unavailable

    tree = parse_formula(definition["formula"], set(definition["inputs"]))
    lineage: list[tuple[int, str]] = []
    try:
        env = {name: _input_value(session, spec, week, lineage) for name, spec in definition["inputs"].items()}
        value = _eval(tree, env)
    except _Unavailable:
        return unavailable(definition["id"], definition.get("unit", ""))
    lineage.sort(key=lambda x: -x[0])
    cites = list(dict.fromkeys(c for _, c in lineage))[:40]
    if not cites:
        return unavailable(definition["id"], definition.get("unit", ""))
    return MetricResult(definition["id"], round(value, 4), lineage=cites, unit=definition.get("unit", ""))


# ── registry ────────────────────────────────────────────────────────────────

_lock = threading.Lock()
# Definitions are per tenant (each tenant has its own database); the global signal
# registries hold the union of ids, and each compute function resolves the current
# tenant's definition at call time.
_defs_by_tenant: dict[str, dict[str, dict]] = {}
_stamps: dict[str, tuple] = {}
_custom_ids: set[str] = set()
BUILTIN_IDS: set[str] = set()


def current_custom_definitions() -> dict[str, dict]:
    from cfo_agent.tenancy import cache_key

    return _defs_by_tenant.get(cache_key(), {})


def _make_fn(mid: str):
    def fn(session, week):
        from cfo_agent.signals import unavailable

        d = current_custom_definitions().get(mid)
        if d is None:
            return unavailable(mid)
        return evaluate(session, d, week)

    fn.__name__ = f"declarative_{mid}"
    return fn


def _seed(session: Session) -> None:
    path = CONFIG_DIR / "metrics.yaml"
    if not path.exists() or session.query(MetricDefinition.id).first() is not None:
        return
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    for raw in data.get("metrics") or []:
        try:
            d = validate_definition(raw, builtin_ids=BUILTIN_IDS)
        except MetricDefinitionError as e:
            logger.error("Skipping seed metric %s: %s", raw.get("id"), e)
            continue
        session.add(MetricDefinition(metric_id=d["id"], definition_json=d, created_by="seed"))
    session.flush()


def active_definitions(session: Session) -> dict[str, dict]:
    rows = session.query(MetricDefinition).order_by(MetricDefinition.id.asc()).all()
    latest: dict[str, MetricDefinition] = {}
    for r in rows:
        latest[r.metric_id] = r
    return {mid: r.definition_json for mid, r in latest.items() if r.active}


def ensure_custom_metrics(session: Session, *, force: bool = False) -> None:
    """Register the current tenant's active declarative metrics (idempotent, cheap)."""
    from cfo_agent import signals
    from cfo_agent.tenancy import cache_key

    with _lock:
        if not BUILTIN_IDS:
            BUILTIN_IDS.update(set(signals.COMPUTE_FNS) - _custom_ids)
        key = cache_key()
        try:
            _seed(session)
            stamp = (session.query(func.max(MetricDefinition.id)).scalar(),
                     session.query(func.count(MetricDefinition.id)).scalar(), str(session.get_bind().url))
        except Exception:  # noqa: BLE001 — table may not exist yet during init
            return
        if stamp == _stamps.get(key) and not force:
            return
        _defs_by_tenant[key] = {m: d for m, d in active_definitions(session).items() if m not in BUILTIN_IDS}
        _stamps[key] = stamp
        union = {m for defs in _defs_by_tenant.values() for m in defs}
        for mid in _custom_ids - union:
            signals.COMPUTE_FNS.pop(mid, None)
            signals.SIGNAL_REGISTRY.pop(mid, None)
            signals.METRIC_META.pop(mid, None)
            signals.ADVERSE_DIRECTION.pop(mid, None)
        for mid in union:
            d = _defs_by_tenant[key].get(mid) or next(defs[mid] for defs in _defs_by_tenant.values() if mid in defs)
            signals.COMPUTE_FNS[mid] = _make_fn(mid)
            signals.SIGNAL_REGISTRY[mid] = f"declarative:{mid}"
            signals.METRIC_META[mid] = {"label": d["label"], "unit": d["unit"], "custom": True,
                                        "description": d.get("description", "")}
            signals.ADVERSE_DIRECTION[mid] = d.get("adverse_direction")
        _custom_ids.clear()
        _custom_ids.update(union)


def visible_metric_ids() -> list[str]:
    """Built-in metrics plus the current tenant's custom ones."""
    return sorted(BUILTIN_IDS | set(current_custom_definitions()))


def save_definition(session: Session, raw: dict, *, user: str) -> MetricDefinition:
    ensure_custom_metrics(session)
    d = validate_definition(raw, builtin_ids=BUILTIN_IDS)
    for name, spec in d["inputs"].items():
        if spec["agg"] == "metric":
            from cfo_agent.signals import COMPUTE_FNS

            if spec["metric"] not in COMPUTE_FNS:
                raise MetricDefinitionError(f"input '{name}': unknown metric '{spec['metric']}'")
    row = MetricDefinition(metric_id=d["id"], definition_json=d, created_by=user)
    session.add(row)
    session.flush()
    ensure_custom_metrics(session, force=True)
    return row


def retire_definition(session: Session, metric_id: str, *, user: str) -> None:
    defs = active_definitions(session)
    if metric_id not in defs:
        raise MetricDefinitionError(f"no active custom metric '{metric_id}'")
    from cfo_agent.kra import get_active_framework

    if metric_id in get_active_framework(session).metric_map:
        raise MetricDefinitionError(f"'{metric_id}' is used by the KRA framework; remove it from its KRAs first")
    session.add(MetricDefinition(metric_id=metric_id, definition_json=defs[metric_id], active=False, created_by=user))
    session.flush()
    ensure_custom_metrics(session, force=True)
