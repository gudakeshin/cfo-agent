"""Secret encryption at rest and password hashing.

Connector secrets are stored as ``enc:v1:<fernet token>``. The Fernet key is derived
from SECRET_KEY, so rotating SECRET_KEY requires re-entering connector secrets (or
running ``reencrypt_secrets`` with the old key first).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os

from cryptography.fernet import Fernet, InvalidToken

from cfo_agent.settings import get_settings

logger = logging.getLogger(__name__)

ENC_PREFIX = "enc:v1:"
PBKDF2_ITERATIONS = 390_000


class ConfigError(RuntimeError):
    pass


def _fernet(secret_key: str | None = None) -> Fernet | None:
    key = secret_key if secret_key is not None else get_settings().secret_key
    if not key:
        return None
    digest = hashlib.sha256(("cfo-agent/secrets/" + key).encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt_secret(plain: str | None) -> str | None:
    if not plain:
        return plain
    if plain.startswith(ENC_PREFIX):
        return plain
    f = _fernet()
    if f is None:
        if not get_settings().demo_mode:
            raise ConfigError("SECRET_KEY is required to store connector secrets outside demo mode")
        return plain  # demo only: stored as-is, startup logs a warning
    return ENC_PREFIX + f.encrypt(plain.encode()).decode()


def decrypt_secret(stored: str | None) -> str | None:
    if not stored or not stored.startswith(ENC_PREFIX):
        return stored
    f = _fernet()
    if f is None:
        raise ConfigError("SECRET_KEY is not set; cannot decrypt stored connector secrets")
    try:
        return f.decrypt(stored[len(ENC_PREFIX):].encode()).decode()
    except InvalidToken as e:
        raise ConfigError("Stored connector secret cannot be decrypted with the current SECRET_KEY") from e


def encrypt_plaintext_secrets(session) -> int:
    """Encrypt any connector secrets still stored in plain text (one-off migration)."""
    from cfo_agent.models import ConnectorConfig

    if _fernet() is None:
        return 0
    n = 0
    for cfg in session.query(ConnectorConfig).all():
        if cfg.secret and not cfg.secret.startswith(ENC_PREFIX):
            cfg.secret = encrypt_secret(cfg.secret)
            n += 1
    if n:
        logger.info("Encrypted %d plaintext connector secret(s)", n)
    return n


def validate_deployment_settings() -> list[str]:
    """Hard errors for an unsafe non-demo configuration; warnings are logged."""
    s = get_settings()
    errors: list[str] = []
    if not s.demo_mode:
        if not s.secret_key or len(s.secret_key) < 32:
            errors.append("SECRET_KEY must be set (≥32 characters) when DEMO_MODE=false")
        if s.auth_mode == "off":
            errors.append("AUTH_MODE=off is only allowed in demo mode; use local or oidc")
    if s.auth_mode == "oidc" and not (s.oidc_issuer and s.oidc_client_id and s.oidc_client_secret):
        errors.append("AUTH_MODE=oidc needs OIDC_ISSUER, OIDC_CLIENT_ID and OIDC_CLIENT_SECRET")
    if s.auth_mode not in ("off", "local", "oidc"):
        errors.append(f"Unknown AUTH_MODE {s.auth_mode!r}")
    if s.auth_mode != "off" and not s.secret_key:
        errors.append("SECRET_KEY is required to sign session cookies when sign-in is enabled")
    from cfo_agent.residency import residency_errors

    errors.extend(residency_errors(s))
    if s.data_residency and s.news_effective_provider == "anthropic":
        logger.warning(
            "Regulatory feed searches via the Anthropic API (public topics only; no client data)"
        )
    if s.data_residency and not s.pii_masking:
        logger.warning("DATA_RESIDENCY is set but PII_MASKING is off — consider enabling it")
    if s.demo_mode and not s.secret_key:
        logger.warning("SECRET_KEY not set — connector secrets are stored unencrypted (demo mode only)")
    return errors


# ── passwords (local sign-in) ────────────────────────────────────────────────

def hash_password(password: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algo, iters, salt_b64, hash_b64 = encoded.split("$")
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(), base64.b64decode(salt_b64), int(iters))
        return hmac.compare_digest(dk, base64.b64decode(hash_b64))
    except Exception:  # noqa: BLE001
        return False
