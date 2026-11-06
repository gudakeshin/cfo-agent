"""Declarative metrics: validation, safe formulas, evaluation, registry, KRA use."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from cfo_agent.api import api_router
from cfo_agent.metrics_dsl import (
    BUILTIN_IDS,
    MetricDefinitionError,
    ensure_custom_metrics,
    evaluate,
    parse_formula,
    validate_definition,
)
from cfo_agent.signals import COMPUTE_FNS, compute_all_signals


def _client():
    app = FastAPI()
    app.include_router(api_router)
    return TestClient(app)


def test_formula_whitelist():
    parse_formula("100 * (a - b) / max(a, 1)", {"a", "b"})
    for bad in ("__import__('os')", "a.b", "[a]", "a if b else 1", "open('x')", "a ** 2", "c + 1"):
        with pytest.raises(MetricDefinitionError):
            parse_formula(bad, {"a", "b"})


def test_validation_messages():
    base = {"id": "x_metric", "label": "X", "unit": "%", "inputs": {"a": {"agg": "sum", "source": "expenses"}}, "formula": "a"}
    validate_definition(base, builtin_ids={"dso_days"})
    for patch, msg in [({"id": "dso_days"}, "built-in"), ({"unit": "furlongs"}, "unit"),
                       ({"inputs": {"a": {"source": "users"}}}, "source"),
                       ({"inputs": {"a": {"source": "expenses", "where": {"password": "x"}}}}, "cannot filter"),
                       ({"inputs": {"a": {"source": "expenses", "window": "forever"}}}, "window")]:
        with pytest.raises(MetricDefinitionError, match=msg):
            validate_definition({**base, **patch}, builtin_ids={"dso_days"})


def test_seeded_metrics_evaluate_from_gl(seeded_erp, db_session):
    ensure_custom_metrics(db_session, force=True)
    assert "gross_margin_pct" in COMPUTE_FNS and "gross_margin_pct" not in BUILTIN_IDS
    week = seeded_erp["weeks"][-2]
    r = COMPUTE_FNS["gross_margin_pct"](db_session, week)
    assert r.available and 30 < r.value < 50
    assert all(c.startswith("erp:erp_gl_entries:") for c in r.lineage)
    # zero denominator → unavailable, never a crash
    d = {"id": "nothing_pct", "label": "n", "unit": "%", "formula": "100 * a / b",
         "inputs": {"a": {"agg": "count", "source": "credit_notes"}, "b": {"agg": "count", "source": "credit_notes"}}}
    assert not evaluate(db_session, validate_definition(d, builtin_ids=set()), week).available


def test_custom_metric_flows_into_signals_once_a_kra_uses_it(seeded_erp, db_session):
    from cfo_agent.kra import create_kra_version, framework_to_dict, get_active_framework

    ensure_custom_metrics(db_session, force=True)
    body = framework_to_dict(get_active_framework(db_session))
    body["faces"]["operator"]["kras"][0]["metrics"].append("gross_margin_pct")
    create_kra_version(db_session, body)
    db_session.commit()
    sigs = compute_all_signals(db_session, seeded_erp["weeks"][-2])
    assert any(s.metric_id == "gross_margin_pct" for s in sigs)


def test_api_preview_save_retire(seeded_erp, db_session):
    c = _client()
    d = {"id": "opex_per_cc", "label": "Plant opex (week)", "unit": "₹ Cr", "adverse_direction": "up",
         "inputs": {"plant": {"agg": "sum", "source": "expenses", "where": {"cost_center": "Plant"}, "window": "week"}},
         "formula": "plant"}
    pv = c.post("/api/metrics/preview", json={"definition": d}).json()
    assert len(pv["points"]) == 8 and pv["points"][-1]["value"] is not None
    assert c.post("/api/metrics/preview", json={"definition": {**d, "formula": "plant +"}}).status_code == 422
    assert c.post("/api/metrics/definitions", json={"definition": d}).status_code == 200
    ids = [m["id"] for m in c.get("/api/metrics").json()]
    assert "opex_per_cc" in ids
    listed = c.get("/api/metrics/definitions").json()["definitions"]
    assert any(x["id"] == "opex_per_cc" for x in listed)
    assert c.post("/api/metrics/definitions/opex_per_cc/retire").status_code == 200
    assert "opex_per_cc" not in [m["id"] for m in c.get("/api/metrics").json()]


def test_industry_templates_are_valid(db_session):
    from cfo_agent.kra import parse_framework, validate_framework_hard
    from cfo_agent.templates import list_templates, load_template
    from cfo_agent.weighting import validate_weighting

    ensure_custom_metrics(db_session, force=True)
    ids = [t["id"] for t in list_templates()]
    assert {"manufacturing", "fmcg", "bfsi_nbfc"} <= set(ids)
    for tid in ids:
        t = load_template(tid)
        fw = parse_framework(t["framework"])
        assert validate_framework_hard(fw, db_session) == [], tid
        if t["weighting_preset"]:
            assert validate_weighting(t["weighting_preset"], kra_ids=fw.kra_ids()) == [], tid
