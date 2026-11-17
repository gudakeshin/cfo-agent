"""Sign-in, roles and the API permission policy.

Roles (least → most privileged):
  viewer    read briefings and digests
  cfo       + give feedback, chat with insights, acknowledge alerts
  delegate  + change the weighting / KRA framework, run digests, manage memory and news
  admin     + data connectors, demo data, audit log, maintenance

Every /api route is checked against POLICY (first match wins) by `authorize`, which is
attached to the whole API router, so a new route is denied to all but admins until it
is added here. Sessions are signed, HttpOnly, SameSite=Lax cookies; mutating requests
must also carry the X-Requested-With header (CSRF defence for cookie auth).
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from dataclasses import asdict, dataclass

import yaml
from fastapi import HTTPException, Request
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from cfo_agent.settings import get_settings

logger = logging.getLogger(__name__)

ROLES = ("viewer", "cfo", "delegate", "admin")
ROLE_RANK = {r: i for i, r in enumerate(ROLES)}

PERMISSION_MIN_ROLE = {
    "public": None,
    "read": "viewer",
    "interact": "cfo",
    "configure": "delegate",
    "administer": "admin",
}

# (methods, path regex under /api, permission). First match wins.
POLICY: list[tuple[set[str], re.Pattern, str]] = [
    ({"GET"}, re.compile(r"^/api/(health|ops/ready)$"), "public"),
    ({"GET", "POST"}, re.compile(r"^/api/auth/"), "public"),
    # Read-only share links: the signed token in the path is the credential
    ({"GET"}, re.compile(r"^/api/shared/[^/]+(/export\.pdf)?$"), "public"),
    # Admin-only reads
    ({"GET"}, re.compile(r"^/api/(connectors|audit)"), "administer"),
    ({"GET"}, re.compile(r"^/api/ops/"), "configure"),
    ({"GET"}, re.compile(r"^/api/memory/"), "configure"),
    # CFO interactions
    ({"POST"}, re.compile(r"^/api/feedback$"), "interact"),
    ({"POST"}, re.compile(r"^/api/chat/\d+(/stream)?$"), "interact"),
    # The Ask box and its history are personal: viewers cannot ask or read it
    ({"POST"}, re.compile(r"^/api/ask(/stream|/clear)?$"), "interact"),
    ({"GET"}, re.compile(r"^/api/ask$"), "interact"),
    ({"POST"}, re.compile(r"^/api/alerts/\d+/ack$"), "interact"),
    ({"POST"}, re.compile(r"^/api/digests/\d+/share$"), "interact"),
    ({"POST"}, re.compile(r"^/api/shares/\d+/revoke$"), "interact"),
    ({"POST", "PUT"}, re.compile(r"^/api/actions(/\d+)?$"), "interact"),
    ({"POST"}, re.compile(r"^/api/briefs$"), "interact"),
    # Delegate configuration
    ({"POST", "PUT"}, re.compile(r"^/api/(weighting|kra)(/|$)"), "configure"),
    ({"POST"}, re.compile(r"^/api/digests/"), "configure"),
    ({"POST", "PUT"}, re.compile(r"^/api/memory/(blocks|search|consolidate)"), "configure"),
    ({"POST", "PUT"}, re.compile(r"^/api/news/"), "configure"),
    ({"POST", "PUT"}, re.compile(r"^/api/metrics/definitions"), "configure"),
    ({"POST"}, re.compile(r"^/api/metrics/preview$"), "configure"),
    ({"POST"}, re.compile(r"^/api/learning/"), "configure"),
    ({"POST"}, re.compile(r"^/api/redlines/backtest$"), "configure"),
    # Everything else that reads is open to any signed-in user
    ({"GET", "HEAD"}, re.compile(r"^/api/"), "read"),
    # Any other mutation (connectors, data/generate, alerts/check, maintenance…) → admin
    ({"POST", "PUT", "DELETE", "PATCH"}, re.compile(r"^/api/"), "administer"),
]

SESSION_COOKIE = "cfo_session"
CSRF_HEADER = "x-requested-with"


@dataclass
class User:
    email: str
    name: str
    role: str

    def can(self, permission: str) -> bool:
        need = PERMISSION_MIN_ROLE[permission]
        return need is None or ROLE_RANK.get(self.role, -1) >= ROLE_RANK[need]


DEMO_USER = User(email="demo@localhost", name="Demo user", role="admin")


def required_permission(method: str, path: str) -> str:
    for methods, pattern, perm in POLICY:
        if method in methods and pattern.search(path):
            return perm
    return "administer"


# ── sessions ─────────────────────────────────────────────────────────────────

def _serializer() -> URLSafeTimedSerializer:
    key = get_settings().secret_key or "demo-only-not-secret"
    return URLSafeTimedSerializer(key, salt="cfo-agent/session")


def make_session_cookie(user: User) -> str:
    from cfo_agent.tenancy import current_tenant_id

    return _serializer().dumps({**asdict(user), "t": current_tenant_id()})


def read_session_cookie(value: str | None) -> User | None:
    if not value:
        return None
    try:
        data = _serializer().loads(value, max_age=get_settings().session_ttl_hours * 3600)
        if data.get("role") not in ROLES:
            return None
        from cfo_agent.tenancy import current_tenant_id

        if data.get("t") != current_tenant_id():
            return None  # a cookie from one tenant never opens another
        return User(email=data["email"], name=data.get("name") or data["email"], role=data["role"])
    except (BadSignature, SignatureExpired, KeyError, TypeError):
        return None


def cookie_kwargs() -> dict:
    s = get_settings()
    return {
        "httponly": True,
        "samesite": "lax",
        "secure": s.public_base_url.startswith("https://"),
        "max_age": s.session_ttl_hours * 3600,
        "path": "/",
    }


def current_user(request: Request) -> User | None:
    if get_settings().auth_mode == "off":
        return DEMO_USER
    return read_session_cookie(request.cookies.get(SESSION_COOKIE))


def authorize(request: Request) -> User | None:
    """Router-level dependency: enforce POLICY for every /api request."""
    method = request.method.upper()
    path = request.url.path
    perm = required_permission(method, path)
    if perm == "public":
        return None
    user = current_user(request)
    if user is None:
        raise HTTPException(401, detail="Sign in required")
    if method not in ("GET", "HEAD", "OPTIONS") and get_settings().auth_mode != "off":
        if request.headers.get(CSRF_HEADER) != "fetch":
            raise HTTPException(403, detail="Missing request header")
    if not user.can(perm):
        request.state.denied = True
        raise HTTPException(403, detail=f"Your role ({user.role}) cannot do this")
    request.state.user = user
    return user


# ── local users ──────────────────────────────────────────────────────────────

def load_local_users() -> dict[str, dict]:
    from pathlib import Path

    from cfo_agent.tenancy import current_tenant

    t = current_tenant()
    path = Path(t.users_file) if t and t.users_file else get_settings().local_users_file
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    users = {}
    for u in data.get("users") or []:
        email = str(u.get("email", "")).strip().lower()
        if email and u.get("role") in ROLES and u.get("password_hash"):
            users[email] = u
    return users


class LoginThrottle:
    """At most 5 failed sign-ins per email+IP in 15 minutes."""

    def __init__(self, limit: int = 5, window_s: int = 900):
        self.limit, self.window = limit, window_s
        self._fails: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def blocked(self, key: str) -> bool:
        with self._lock:
            now = time.time()
            fails = [t for t in self._fails.get(key, []) if now - t < self.window]
            self._fails[key] = fails
            return len(fails) >= self.limit

    def fail(self, key: str) -> None:
        with self._lock:
            self._fails.setdefault(key, []).append(time.time())

    def clear(self, key: str) -> None:
        with self._lock:
            self._fails.pop(key, None)


throttle = LoginThrottle()


# ── OIDC role mapping ────────────────────────────────────────────────────────

def role_from_claims(claims: dict) -> str:
    """Map the IdP's group/role claim to the highest matching app role."""
    s = get_settings()
    try:
        mapping = {str(k): str(v) for k, v in json.loads(s.oidc_role_map or "{}").items()}
    except json.JSONDecodeError:
        logger.error("OIDC_ROLE_MAP is not valid JSON; using default role")
        mapping = {}
    raw = claims.get(s.oidc_roles_claim) or []
    values = [raw] if isinstance(raw, str) else list(raw)
    roles = [mapping[v] for v in values if mapping.get(v) in ROLES]
    if not roles:
        return s.oidc_default_role if s.oidc_default_role in ROLES else "viewer"
    return max(roles, key=lambda r: ROLE_RANK[r])
