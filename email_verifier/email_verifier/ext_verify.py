"""External email verification via Hunter.io and ZeroBounce.

These services can resolve catch-all ambiguity that SMTP alone cannot
(e.g. Google Workspace domains that accept all RCPT TO with 250).
"""

from __future__ import annotations

import os
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

import httpx

HUNTER_API_KEY = os.environ.get("HUNTER_API_KEY", "")
ZEROBOUNCE_API_KEY = os.environ.get("ZEROBOUNCE_API_KEY", "")


@dataclass
class ExtResult:
    email: str
    status: str  # "valid", "invalid", "catch_all", "unknown", "error"
    service: str


@dataclass
class CreditStatus:
    hunter_remaining: int | None = None
    hunter_total: int | None = None
    zerobounce_remaining: int | None = None


def get_credits() -> CreditStatus:
    """Query remaining credits from both services."""
    cs = CreditStatus()
    if HUNTER_API_KEY:
        try:
            r = httpx.get("https://api.hunter.io/v2/account",
                          params={"api_key": HUNTER_API_KEY}, timeout=10)
            if r.status_code == 200:
                v = r.json().get("data", {}).get("requests", {}).get("verifications", {})
                cs.hunter_remaining = v.get("remaining")
                cs.hunter_total = v.get("available")
        except Exception:
            pass
    if ZEROBOUNCE_API_KEY:
        try:
            r = httpx.get("https://api.zerobounce.net/v2/getcredits",
                          params={"api_key": ZEROBOUNCE_API_KEY}, timeout=10)
            if r.status_code == 200:
                cs.zerobounce_remaining = int(r.json().get("Credits", -1))
        except Exception:
            pass
    return cs


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


def ext_verify_patterns(
    pattern_hits: list[dict],
    budget: int | None = None,
    timeout: int = 15,
) -> dict:
    """Verify pattern hits using external APIs in concurrent specificity tiers.

    Groups patterns by specificity and verifies each tier in parallel.
    Stops after the first tier that produces a valid hit, saving both
    wall-clock time and credits on lower tiers.
    """
    from email_verifier.ms_probe import PATTERN_SPECIFICITY

    credits = get_credits()
    available = 0
    if credits.hunter_remaining and credits.hunter_remaining > 0:
        available += credits.hunter_remaining
    if credits.zerobounce_remaining and credits.zerobounce_remaining > 0:
        available += credits.zerobounce_remaining

    if budget is not None:
        available = min(available, budget)

    if available <= 0:
        return {"credits_used": 0, "credits_remaining": credits, "skipped": True,
                "reason": "No verification credits remaining"}

    # Group by specificity tier (descending)
    tiers: dict[int, list[tuple[int, dict]]] = defaultdict(list)
    for idx, h in enumerate(pattern_hits):
        spec = PATTERN_SPECIFICITY.get(h["pattern"], 0)
        tiers[spec].append((idx, h))

    used = 0

    for spec in sorted(tiers.keys(), reverse=True):
        tier = tiers[spec]
        if used + len(tier) > available:
            tier = tier[:available - used]
        if not tier:
            break

        # Verify entire tier in parallel
        results: dict[int, ExtResult] = {}
        with ThreadPoolExecutor(max_workers=min(len(tier), 10)) as pool:
            futures = {
                pool.submit(ext_verify, h["email"], timeout): idx
                for idx, h in tier
            }
            for future in as_completed(futures):
                idx = futures[future]
                results[idx] = future.result()

        used += len(tier)
        found_valid = False

        for idx, ev in results.items():
            if ev.status == "valid":
                pattern_hits[idx]["exists"] = True
                pattern_hits[idx]["ext_service"] = ev.service
                found_valid = True
            elif ev.status == "invalid":
                pattern_hits[idx]["exists"] = False
                pattern_hits[idx]["ext_service"] = ev.service
            else:
                pattern_hits[idx]["ext_service"] = ev.service

        if found_valid:
            break

    final_credits = get_credits()
    return {
        "credits_used": used,
        "credits_remaining": final_credits,
        "skipped": False,
    }
