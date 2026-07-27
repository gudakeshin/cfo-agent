"""WS3 — user-configurable KRA CRUD, versioning, restore, weighting reconcile."""

from __future__ import annotations

import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from cfo_agent.api import api_router
from cfo_agent.kra import get_active_framework, get_active_kra_version
from cfo_agent.memory.blocks import ensure_seed_blocks, get_block
from cfo_agent.models import KraVersion, WeightingVersion
from cfo_agent.weighting import get_active_weighting


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(api_router)
    return TestClient(app)


def _framework_body(db_session) -> dict:
    fw = get_active_framework(db_session)
    faces: dict = {}
    for k in fw.kras.values():
        faces.setdefault(k.face, {"kras": []})["kras"].append(
            {
                "id": k.id,
                "name": k.name,
                "grounding": k.grounding,
                "metrics": list(k.metrics),
                "concerns": list(k.concerns),
                **({"note": k.note} if k.note else {}),
            }
        )
    return {
        "label": fw.label + " (edited)",
        "india_regulatory_context": fw.india_regulatory_context,
        "faces": faces,
        "relationships": [
            {
                "id": r.id,
                "from": r.from_kra,
                "to": r.to_kra,
                "description": r.description,
            }
            for r in fw.relationships
        ],
    }


def test_put_creates_version_resyncs_block_rebuilds_metric_map(db_session):
    ensure_seed_blocks(db_session)
    db_session.commit()
    before = db_session.query(KraVersion).count()
    body = _framework_body(db_session)
    # Tweak a KRA name
    first_face = next(iter(body["faces"]))
    body["faces"][first_face]["kras"][0]["name"] = "Renamed KRA for test"

    client = _client()
    res = client.put("/api/kra", json=body)
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["version"]["kras"]
    assert db_session.query(KraVersion).count() == before + 1
    assert "metric_map" in data["version"]
    assert "registry_metrics" in data["version"]

    block = get_block(db_session, "kra_framework")
    assert "Renamed KRA for test" in block.content


def test_hard_errors_422_no_row(db_session):
    before = db_session.query(KraVersion).count()
    body = _framework_body(db_session)
    # Duplicate KRA id across faces
    faces = list(body["faces"].keys())
    if len(faces) >= 2:
        kid = body["faces"][faces[0]]["kras"][0]["id"]
        body["faces"][faces[1]]["kras"].append(
            {
                "id": kid,
                "name": "Dup",
                "grounding": "none",
                "metrics": [],
                "concerns": [],
            }
        )
    else:
        body["faces"][faces[0]]["kras"].append(
            {
                "id": body["faces"][faces[0]]["kras"][0]["id"],
                "name": "Dup",
                "grounding": "none",
                "metrics": [],
                "concerns": [],
            }
        )

    client = _client()
    res = client.put("/api/kra", json=body)
    assert res.status_code == 422
    assert db_session.query(KraVersion).count() == before


def test_count_floors_return_warnings_not_422(db_session):
    body = _framework_body(db_session)
    # Strip relationships below floor
    body["relationships"] = body["relationships"][:2]
    client = _client()
    res = client.put("/api/kra", json=body)
    assert res.status_code == 200, res.text
    assert res.json().get("warnings")
    assert any("relationship" in w.lower() for w in res.json()["warnings"])


def test_relationship_edits_validated(db_session):
    body = _framework_body(db_session)
    body["relationships"].append(
        {
            "id": "REL-BAD",
            "from": "NO_SUCH_KRA",
            "to": body["relationships"][0]["to"] if body["relationships"] else "STW-1",
            "description": "bad",
        }
    )
    client = _client()
    res = client.put("/api/kra", json=body)
    assert res.status_code == 422


def test_ranked_kra_delete_reconciles_weighting(db_session):
    ensure_seed_blocks(db_session)
    db_session.commit()
    wv = get_active_weighting(db_session)
    ranks = list(wv.content_json.get("kra_ranks") or [])
    assert ranks, "seed weighting should have kra_ranks"
    victim = ranks[0]

    body = _framework_body(db_session)
    # Remove victim KRA from faces and relationships
    for face in body["faces"].values():
        face["kras"] = [k for k in face["kras"] if k["id"] != victim]
    body["relationships"] = [
        r for r in body["relationships"] if r["from"] != victim and r["to"] != victim
    ]

    client = _client()
    res = client.put("/api/kra", json=body)
    assert res.status_code == 200, res.text
    data = res.json()
    assert data.get("reconciled_weighting") is not None
    db_session.expire_all()
    active = get_active_weighting(db_session)
    assert victim not in (active.content_json.get("kra_ranks") or [])
    assert "(KRA sync)" in active.label


def test_restore_creates_new_version(db_session):
    body = _framework_body(db_session)
    first_face = next(iter(body["faces"]))
    original_name = body["faces"][first_face]["kras"][0]["name"]
    body["faces"][first_face]["kras"][0]["name"] = "Temporary rename"

    client = _client()
    res = client.put("/api/kra", json=body)
    assert res.status_code == 200
    old_id = res.json()["db_version_id"]

    # Get previous version id
    versions = client.get("/api/kra/versions").json()
    assert len(versions) >= 2
    restore_id = versions[1]["id"]  # older

    res2 = client.post(f"/api/kra/versions/{restore_id}/restore")
    assert res2.status_code == 200, res2.text
    assert res2.json()["new_version_id"] != restore_id
    assert res2.json()["restored_from"] == restore_id


def test_db_wins_over_yaml_after_edits(db_session):
    body = _framework_body(db_session)
    first_face = next(iter(body["faces"]))
    body["faces"][first_face]["kras"][0]["name"] = "DB-backed name XYZ"

    client = _client()
    assert client.put("/api/kra", json=body).status_code == 200

    fw = get_active_framework(db_session)
    names = {k.name for k in fw.kras.values()}
    assert "DB-backed name XYZ" in names

    # YAML file still has old content — framework must come from DB
    from cfo_agent.settings import CONFIG_DIR

    yaml_data = yaml.safe_load((CONFIG_DIR / "kra_framework.yaml").read_text())
    yaml_names = set()
    for face in yaml_data.get("faces", {}).values():
        for k in face.get("kras", []):
            yaml_names.add(k["name"])
    assert "DB-backed name XYZ" not in yaml_names


def test_orchestrator_uses_latest_kra_version(db_session):
    """After a KRA edit, get_active_kra_version (what the orchestrator stamps) is the new row;
    upsert_kra_version is seed-only and must not shadow it."""
    import inspect

    from cfo_agent.kra import upsert_kra_version
    from cfo_agent.pipeline import orchestrator as orch

    body = _framework_body(db_session)
    first_face = next(iter(body["faces"]))
    body["faces"][first_face]["kras"][0]["name"] = "Orchestrator-visible rename"

    client = _client()
    res = client.put("/api/kra", json=body)
    assert res.status_code == 200
    new_id = res.json()["db_version_id"]

    active = get_active_kra_version(db_session)
    assert active.id == new_id
    assert "Orchestrator-visible rename" in active.content_yaml

    before = db_session.query(KraVersion).count()
    upsert_kra_version(db_session)  # seed-only when table non-empty
    db_session.commit()
    assert db_session.query(KraVersion).count() == before
    assert get_active_kra_version(db_session).id == new_id

    src = inspect.getsource(orch.run_pipeline)
    assert "get_active_kra_version" in src
    assert "upsert_kra_version" not in src


def test_get_kra_includes_registry_metrics(db_session):
    client = _client()
    res = client.get("/api/kra")
    assert res.status_code == 200
    assert "registry_metrics" in res.json()
    assert isinstance(res.json()["registry_metrics"], list)
    assert len(res.json()["registry_metrics"]) > 0
