"""Pseudonymise customer and vendor names before any LLM call (PII_MASKING=true).

Names in prompts, tool results and memory text become stable aliases
(``CUSTOMER_03``, ``VENDOR_02``); the model's output and tool inputs are mapped back
before anything is stored or shown. Aliases are stable for a given dataset, so
memory written in one run stays consistent in the next.
"""

from __future__ import annotations

import re
import threading
import time
from typing import Any

_lock = threading.Lock()
_cache: dict[str, tuple[float, "AliasMap"]] = {}
TTL_S = 300


class AliasMap:
    def __init__(self, names: dict[str, str]):
        # real name -> alias, longest names first so "Apex Retail Pvt Ltd" wins over "Apex Retail"
        self.forward = dict(sorted(names.items(), key=lambda kv: -len(kv[0])))
        self.reverse = {v: k for k, v in self.forward.items()}
        self._fwd_re = (
            re.compile("|".join(rf"(?<!\w){re.escape(n)}(?!\w)" for n in self.forward), re.IGNORECASE)
            if self.forward
            else None
        )
        self._rev_re = (
            re.compile("|".join(rf"\b{re.escape(a)}\b" for a in self.reverse), re.IGNORECASE)
            if self.reverse
            else None
        )
        self._lower_fwd = {k.lower(): v for k, v in self.forward.items()}
        self._upper_rev = {k.upper(): v for k, v in self.reverse.items()}
        self.max_alias_len = max((len(a) for a in self.reverse), default=0)

    def mask(self, s: str) -> str:
        if not self._fwd_re or not s:
            return s
        return self._fwd_re.sub(lambda m: self._lower_fwd[m.group(0).lower()], s)

    def unmask(self, s: str) -> str:
        if not self._rev_re or not s:
            return s
        return self._rev_re.sub(lambda m: self._upper_rev[m.group(0).upper()], s)


def build_alias_map(session) -> AliasMap:
    from cfo_agent.models import ErpCustomer, ErpPurchaseInvoice

    names: dict[str, str] = {}
    for i, c in enumerate(session.query(ErpCustomer).order_by(ErpCustomer.business_key).all(), start=1):
        if c.name and len(c.name.strip()) >= 3:
            names[c.name.strip()] = f"CUSTOMER_{i:02d}"
    vendors = sorted({v for (v,) in session.query(ErpPurchaseInvoice.vendor_name).distinct() if v})
    for i, v in enumerate(vendors, start=1):
        if len(v.strip()) >= 3 and v.strip() not in names:
            names[v.strip()] = f"VENDOR_{i:02d}"
    return AliasMap(names)


def current_map() -> AliasMap | None:
    """The alias map when masking is on (cached for a few minutes), else None."""
    from cfo_agent.settings import get_settings

    if not get_settings().pii_masking:
        return None
    from cfo_agent.tenancy import cache_key

    key = cache_key()
    with _lock:
        hit = _cache.get(key)
        if hit and time.time() - hit[0] < TTL_S:
            return hit[1]
        from cfo_agent.db import get_session

        session = get_session()
        try:
            amap = build_alias_map(session)
        finally:
            session.close()
        _cache[key] = (time.time(), amap)
        return amap


def invalidate() -> None:
    from cfo_agent.tenancy import cache_key

    with _lock:
        _cache.pop(cache_key(), None)


def deep_apply(obj: Any, fn) -> Any:
    """Apply a str->str function to every string inside nested dicts/lists."""
    if isinstance(obj, str):
        return fn(obj)
    if isinstance(obj, list):
        return [deep_apply(v, fn) for v in obj]
    if isinstance(obj, tuple):
        return tuple(deep_apply(v, fn) for v in obj)
    if isinstance(obj, dict):
        return {k: deep_apply(v, fn) for k, v in obj.items()}
    return obj


class StreamUnmasker:
    """Unmask streamed text whose aliases may be split across chunks: holds back a
    short tail that could be the start of an alias until the next chunk arrives."""

    def __init__(self, amap: AliasMap):
        self.amap = amap
        self.buf = ""

    def feed(self, chunk: str) -> str:
        self.buf += chunk
        hold = 0
        upper = self.buf.upper()
        for n in range(1, min(self.amap.max_alias_len, len(self.buf)) + 1):
            tail = upper[-n:]
            if any(a.startswith(tail) for a in self.amap._upper_rev):
                hold = n
        out, self.buf = (self.buf[:-hold], self.buf[-hold:]) if hold else (self.buf, "")
        return self.amap.unmask(out)

    def flush(self) -> str:
        out, self.buf = self.amap.unmask(self.buf), ""
        return out
