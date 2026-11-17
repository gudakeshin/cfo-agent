"""Sign-in, roles, CSRF header, audit trail and deployment-safety checks."""

from __future__ import annotations

import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from cfo_agent.api import api_router
from cfo_agent.auth import User, required_permission, role_from_claims
from cfo_agent.models import AuditEvent
from cfo_agent.security import (
    decrypt_secret,
    encrypt_secret,
    hash_password,
    validate_deployment_settings,
    verify_password,
)
from cfo_agent.settings import get_settings

KEY = "x" * 40
PASSWORDS = {"viewer": "viewer-password-1", "cfo": "cfo-password-123", "delegate": "delegate-pw-1234", "admin": "admin-password-12"}


@pytest.fixture()
def local_auth(db_session, tmp_path, monkeypatch):
    users = [
        {"email": f"{r}@client.com", "name": r.title(), "role": r, "password_hash": hash_password(pw)}
        for r, pw in PASSWORDS.items()
    ]
    f = tmp_path / "users.yaml"
    f.write_text(yaml.safe_dump({"users": users}))
    monkeypatch.setenv("AUTH_MODE", "local")
    monkeypatch.setenv("SECRET_KEY", KEY)
    monkeypatch.setenv("LOCAL_USERS_FILE", str(f))
    get_settings.cache_clear()
    from cfo_agent.auth import throttle

    throttle._fails.clear()
    yield
    get_settings.cache_clear()


def _app_client() -> TestClient:
    from cfo_agent.main import request_context  # noqa: F401  (ensures middleware module imports)

    app = FastAPI()
    app.include_router(api_router)
    # Reuse the real request middleware so audit rows are written
    from cfo_agent import main as m

    app.middleware("http")(m.request_context)
    return TestClient(app)


def _login(client: TestClient, role: str):
    return client.post(
        "/api/auth/login",
        json={"email": f"{role}@client.com", "password": PASSWORDS[role]},
        headers={"X-Requested-With": "fetch"},
    )


H = {"X-Requested-With": "fetch"}


def test_policy_table():
    assert required_permission("GET", "/api/briefing") == "read"
    assert required_permission("POST", "/api/feedback") == "interact"
    assert required_permission("POST", "/api/chat/12/stream") == "interact"
    assert required_permission("PUT", "/api/weighting") == "configure"
    assert required_permission("POST", "/api/digests/generate") == "configure"
    assert required_permission("GET", "/api/connectors") == "administer"
    assert required_permission("POST", "/api/data/generate") == "administer"
    assert required_permission("POST", "/api/something-new") == "administer"  # fail closed


def test_requires_sign_in(local_auth):
    c = _app_client()
    assert c.get("/api/briefing").status_code == 401
    me = c.get("/api/auth/me").json()
    assert me["auth_mode"] == "local" and me["user"] is None


def test_wrong_password_and_throttle(local_auth):
    c = _app_client()
    for _ in range(5):
        r = c.post("/api/auth/login", json={"email": "cfo@client.com", "password": "nope"}, headers=H)
        assert r.status_code == 401
    r = c.post("/api/auth/login", json={"email": "cfo@client.com", "password": PASSWORDS["cfo"]}, headers=H)
    assert r.status_code == 429


@pytest.mark.parametrize(
    "role,expect",
    [
        ("viewer", {"read": 200, "feedback": 403, "weighting": 403, "connectors": 403}),
        ("cfo", {"read": 200, "feedback": 422, "weighting": 403, "connectors": 403}),
        ("delegate", {"read": 200, "feedback": 422, "weighting": 422, "connectors": 403}),
        ("admin", {"read": 200, "feedback": 422, "weighting": 422, "connectors": 200}),
    ],
)
def test_role_matrix(local_auth, role, expect):
    c = _app_client()
    assert _login(c, role).status_code == 200
    assert c.get("/api/briefing").status_code == expect["read"]
    # 422 = allowed through the policy, then rejected for the empty body
    assert c.post("/api/feedback", json={}, headers=H).status_code == expect["feedback"]
    assert c.put("/api/weighting", json={}, headers=H).status_code == expect["weighting"]
    assert c.get("/api/connectors").status_code == expect["connectors"]


def test_mutation_needs_csrf_header(local_auth):
    c = _app_client()
    _login(c, "admin")
    r = c.put("/api/weighting", json={})
    assert r.status_code == 403 and "header" in r.json()["detail"].lower()


def test_tampered_cookie_rejected(local_auth):
    c = _app_client()
    _login(c, "viewer")
    c.cookies.set("cfo_session", c.cookies.get("cfo_session")[:-3] + "abc")
    assert c.get("/api/briefing").status_code == 401


def test_audit_trail(local_auth, db_session):
    c = _app_client()
    _login(c, "cfo")
    c.put("/api/weighting", json={}, headers=H)  # denied
    c.post("/api/feedback", json={}, headers=H)  # change attempt (422)
    c.get("/api/briefing")  # view
    db_session.expire_all()
    actions = [(e.action, e.user_email) for e in db_session.query(AuditEvent).order_by(AuditEvent.id)]
    assert ("sign_in", "cfo@client.com") in actions
    assert ("denied", "cfo@client.com") in actions
    assert ("change", "cfo@client.com") in actions
    assert ("view", "cfo@client.com") in actions


def test_logout(local_auth):
    c = _app_client()
    _login(c, "cfo")
    c.post("/api/auth/logout", headers=H)
    assert c.get("/api/briefing").status_code == 401


def test_oidc_role_mapping(monkeypatch):
    monkeypatch.setenv("OIDC_ROLE_MAP", '{"CFO-Office": "cfo", "Fin-Delegates": "delegate"}')
    monkeypatch.setenv("OIDC_ROLES_CLAIM", "groups")
    get_settings.cache_clear()
    assert role_from_claims({"groups": ["Everyone", "CFO-Office", "Fin-Delegates"]}) == "delegate"
    assert role_from_claims({"groups": "CFO-Office"}) == "cfo"
    assert role_from_claims({"groups": ["Everyone"]}) == "viewer"
    get_settings.cache_clear()


def test_secrets_encrypted_at_rest(monkeypatch):
    monkeypatch.setenv("SECRET_KEY", KEY)
    get_settings.cache_clear()
    token = encrypt_secret("s3cr3t-value")
    assert token.startswith("enc:v1:") and "s3cr3t" not in token
    assert decrypt_secret(token) == "s3cr3t-value"
    get_settings.cache_clear()


def test_password_hash_roundtrip():
    h = hash_password("correct horse battery")
    assert verify_password("correct horse battery", h)
    assert not verify_password("wrong", h)


def test_unsafe_production_config_rejected(monkeypatch):
    monkeypatch.setenv("DEMO_MODE", "false")
    monkeypatch.setenv("AUTH_MODE", "off")
    monkeypatch.delenv("SECRET_KEY", raising=False)
    get_settings.cache_clear()
    problems = " ".join(validate_deployment_settings())
    assert "SECRET_KEY" in problems and "AUTH_MODE=off" in problems
    get_settings.cache_clear()


def test_demo_data_generation_blocked_outside_demo(db_session, monkeypatch):
    monkeypatch.setenv("DEMO_MODE", "false")
    get_settings.cache_clear()
    c = TestClient(FastAPI())
    app = FastAPI()
    app.include_router(api_router)
    c = TestClient(app)
    assert c.post("/api/data/generate", json={}).status_code == 403
    get_settings.cache_clear()


def test_spa_path_traversal(tmp_path, monkeypatch):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("INDEX")
    (tmp_path / "secret.txt").write_text("TOP SECRET")
    monkeypatch.setenv("FRONTEND_DIST", str(dist))
    get_settings.cache_clear()
    from cfo_agent.main import _mount_frontend

    app = FastAPI()
    _mount_frontend(app)
    c = TestClient(app)
    for p in ("/../secret.txt", "/%2e%2e/secret.txt", "/..%2fsecret.txt"):
        assert "TOP SECRET" not in c.get(p).text
    get_settings.cache_clear()


def test_user_dataclass_permissions():
    assert User("a", "a", "cfo").can("interact") and not User("a", "a", "cfo").can("configure")
