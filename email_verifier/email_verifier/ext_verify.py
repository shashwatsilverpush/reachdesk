"""External email verification via Hunter.io and ZeroBounce.

These services can resolve catch-all ambiguity that SMTP alone cannot
(e.g. Google Workspace domains that accept all RCPT TO with 250).
"""

from __future__ import annotations

import os
from dataclasses import dataclass

import httpx

HUNTER_API_KEY = os.environ.get("HUNTER_API_KEY", "")
ZEROBOUNCE_API_KEY = os.environ.get("ZEROBOUNCE_API_KEY", "")


@dataclass
class ExtResult:
    email: str
    status: str  # "valid", "invalid", "catch_all", "unknown", "error"
    service: str


def _hunter_verify(email: str, timeout: int = 15) -> ExtResult:
    if not HUNTER_API_KEY:
        return ExtResult(email, "error", "hunter")
    try:
        r = httpx.get(
            "https://api.hunter.io/v2/email-verifier",
            params={"email": email, "api_key": HUNTER_API_KEY},
            timeout=timeout,
        )
        if r.status_code == 429:
            return ExtResult(email, "error", "hunter")
        if r.status_code != 200:
            return ExtResult(email, "error", "hunter")
        data = r.json().get("data", {})
        status = data.get("status", "")
        if status in ("valid", "webmail"):
            return ExtResult(email, "valid", "hunter")
        if status == "invalid":
            return ExtResult(email, "invalid", "hunter")
        return ExtResult(email, "unknown", "hunter")
    except Exception:
        return ExtResult(email, "error", "hunter")


def _zerobounce_verify(email: str, timeout: int = 15) -> ExtResult:
    if not ZEROBOUNCE_API_KEY:
        return ExtResult(email, "error", "zerobounce")
    try:
        r = httpx.get(
            "https://api.zerobounce.net/v2/validate",
            params={"email": email, "api_key": ZEROBOUNCE_API_KEY},
            timeout=timeout,
        )
        if r.status_code != 200:
            return ExtResult(email, "error", "zerobounce")
        data = r.json()
        status = data.get("status", "")
        if status == "valid":
            return ExtResult(email, "valid", "zerobounce")
        if status == "invalid":
            return ExtResult(email, "invalid", "zerobounce")
        if status == "catch-all":
            return ExtResult(email, "catch_all", "zerobounce")
        return ExtResult(email, "unknown", "zerobounce")
    except Exception:
        return ExtResult(email, "error", "zerobounce")


def ext_verify(email: str, timeout: int = 15) -> ExtResult:
    """Verify an email via external services (Hunter primary, ZeroBounce fallback)."""
    result = _hunter_verify(email, timeout)
    if result.status not in ("error", "unknown"):
        return result
    zb = _zerobounce_verify(email, timeout)
    if zb.status != "error":
        return zb
    return result
