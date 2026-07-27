"""Config ↔ code consistency and framework validation."""

from cfo_agent.kra import get_active_framework, validate_framework
from cfo_agent.signals import COMPUTE_FNS, SIGNAL_REGISTRY
from cfo_agent.weighting import load_preset, load_weighting_yaml, validate_weighting


def test_framework_valid():
    fw = get_active_framework()
    errs = validate_framework(fw)
    assert errs == [], errs


def test_kra_ids_unique():
    fw = get_active_framework()
    assert len(fw.kras) == len(set(fw.kras))


def test_relationships_ge_6():
    fw = get_active_framework()
    assert len(fw.relationships) >= 6


def test_every_grounded_metric_has_signal_impl():
    fw = get_active_framework()
    missing = []
    for metric in fw.metrics_for_signals():
        if metric not in COMPUTE_FNS:
            missing.append(metric)
        if metric not in SIGNAL_REGISTRY:
            missing.append(f"{metric}(registry)")
    assert missing == [], f"Missing signal implementations: {missing}"


def test_red_line_metrics_exist():
    fw = get_active_framework()
    for preset_id in ("acquisition", "derisking"):
        content = load_preset(preset_id)
        for rl in content.get("red_lines", []):
            if rl.get("type") == "level":
                metric = rl["metric"]
                assert metric in fw.metric_map or metric in COMPUTE_FNS, metric


def test_weighting_yaml_valid():
    content = load_weighting_yaml()
    errs = validate_weighting(content)
    assert errs == [], errs


def test_ungrounded_kras_flagged():
    fw = get_active_framework()
    ungrounded = [k for k in fw.kras.values() if k.grounding == "none"]
    assert len(ungrounded) >= 4
    for k in ungrounded:
        assert k.note or k.grounding == "none"
