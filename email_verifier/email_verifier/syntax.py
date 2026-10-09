"""Stage 1 — Syntax validation, normalization, flag detection, and typo suggestion."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from email_verifier.lists import (
    DISPOSABLE_DOMAINS,
    FREE_PROVIDERS,
    POPULAR_DOMAINS,
    ROLE_ACCOUNTS,
    get_disposable_domains,
    get_free_providers,
    get_popular_domains,
    get_role_accounts,
)

EMAIL_RE = re.compile(
    r"^[a-zA-Z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?(?:\.[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?)*$"
)


@dataclass
class SyntaxResult:
    """Outcome of the syntax validation stage."""

    valid: bool
    email: str
    local: str = ""
    domain: str = ""
    flags: list[str] = field(default_factory=list)
    suggestion: str | None = None
    reason: str = ""


def _levenshtein(a: str, b: str) -> int:
    """Compute Levenshtein edit distance between two strings."""
    if len(a) < len(b):
        return _levenshtein(b, a)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a):
        curr = [i + 1]
        for j, cb in enumerate(b):
            cost = 0 if ca == cb else 1
            curr.append(min(curr[j] + 1, prev[j + 1] + 1, prev[j] + cost))
        prev = curr
    return prev[-1]


def _typo_suggestion(domain: str, popular: set[str]) -> str | None:
    """Find the closest popular domain within Levenshtein distance <= 2."""
    if domain in popular:
        return None
    best: str | None = None
    best_dist = 3
    for p in sorted(popular):
        d = _levenshtein(domain, p)
        if d < best_dist:
            best_dist = d
            best = p
    return best


def _to_punycode(domain: str) -> tuple[str, str | None]:
    """Convert an IDN domain to punycode. Returns (punycode, original_or_None)."""
    try:
        puny = domain.encode("idna").decode("ascii")
    except (UnicodeError, UnicodeDecodeError):
        return domain, None
    if puny != domain:
        return puny, domain
    return domain, None


def check_syntax(
    email: str,
    overrides: dict | None = None,
) -> SyntaxResult:
    """Validate email syntax, normalize, compute flags, and suggest typo fixes.

    Returns a SyntaxResult indicating whether the address passed syntax
    checks and providing normalized local/domain parts, flags, and an
    optional typo suggestion.
    """
    email = email.strip()

    if "@" not in email:
        return SyntaxResult(valid=False, email=email, reason="syntax")

    local, domain = email.rsplit("@", 1)

    # Normalize: lowercase and convert IDN to punycode before validation
    local = local.lower()
    domain = domain.lower()
    domain, original_domain = _to_punycode(domain)

    normalized = f"{local}@{domain}"

    if not EMAIL_RE.match(normalized):
        return SyntaxResult(valid=False, email=email, reason="syntax")

    if len(normalized) > 254:
        return SyntaxResult(valid=False, email=email, reason="syntax")
    if len(local) > 64:
        return SyntaxResult(valid=False, email=email, reason="syntax")
    if len(domain) > 253:
        return SyntaxResult(valid=False, email=email, reason="syntax")
    if ".." in normalized:
        return SyntaxResult(valid=False, email=email, reason="syntax")
    if local.startswith(".") or local.endswith("."):
        return SyntaxResult(valid=False, email=email, reason="syntax")

    flags: list[str] = []
    role_accounts = get_role_accounts(overrides)
    disposable_domains = get_disposable_domains(overrides)
    free_providers = get_free_providers(overrides)

    if local in role_accounts:
        flags.append("role")
    if domain in disposable_domains:
        flags.append("disposable")
    if domain in free_providers:
        flags.append("free")

    popular = get_popular_domains(overrides)
    suggestion = _typo_suggestion(domain, popular)
    if suggestion:
        suggestion = f"{local}@{suggestion}"

    return SyntaxResult(
        valid=True,
        email=normalized,
        local=local,
        domain=domain,
        flags=flags,
        suggestion=suggestion,
    )
