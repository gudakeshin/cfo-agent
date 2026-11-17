"""Secret references: a config value may point at a secret instead of containing it.

    env:ACME_DATABASE_URL        read from the process environment
    file:/run/secrets/acme_db    read from a file (Docker / Kubernetes secrets, a mounted vault agent)
    anything else                used as written (fine for dev and SQLite; not for passwords)

The tenant registry stores references only, so the control database never holds a
client's database password or SSO secret."""

from __future__ import annotations

import os
import re
from pathlib import Path


class SecretError(ValueError):
    pass


def is_reference(value: str) -> bool:
    return value.startswith(("env:", "file:"))


def resolve_secret(value: str) -> str:
    if value.startswith("env:"):
        name = value[4:].strip()
        got = os.environ.get(name)
        if not got:
            raise SecretError(f"environment variable {name} is not set")
        return got
    if value.startswith("file:"):
        path = Path(value[5:].strip())
        try:
            got = path.read_text(encoding="utf-8").strip()
        except OSError as e:
            raise SecretError(f"cannot read secret file {path}: {e.strerror or e}") from e
        if not got:
            raise SecretError(f"secret file {path} is empty")
        return got
    return value


_URL_PASSWORD = re.compile(r"(://[^:/@\s]+:)([^@\s]+)(@)")


def mask(value: str) -> str:
    """A reference as written, or a literal URL with its password hidden."""
    return value if is_reference(value) else _URL_PASSWORD.sub(r"\1***\3", value)


def has_inline_password(value: str) -> bool:
    return not is_reference(value) and bool(_URL_PASSWORD.search(value))
