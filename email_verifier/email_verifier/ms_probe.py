"""Microsoft 365 account existence probe via GetCredentialType.

Microsoft's login flow exposes an unauthenticated endpoint that reveals
whether an email address corresponds to a real Azure AD / M365 account.
This resolves catch-all ambiguity for domains hosted on Exchange Online.

Response field ``IfExistsResult``:
  0 → account exists
  1 → account does not exist
  5 → account exists, federated to another IdP
  6 → account exists, domain-level hints
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

_ENDPOINT = "https://login.microsoftonline.com/common/GetCredentialType"
_EXISTS_CODES = {0, 5, 6}


def is_m365_mx(mx_host: str) -> bool:
    """Check whether an MX host belongs to Microsoft 365."""
    mx = mx_host.lower().rstrip(".")
    return mx.endswith(".mail.protection.outlook.com") or mx.endswith(".mail.eo.outlook.com")


def check_m365_account(email: str, timeout: int = 10) -> bool | None:
    """Query Microsoft's GetCredentialType for account existence.

    Returns:
        True  — account exists in Azure AD / M365.
        False — account does not exist.
        None  — probe failed or was throttled.
    """
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(_ENDPOINT, json={"Username": email})
            if resp.status_code != 200:
                return None
            data = resp.json()
    except Exception:
        return None

    if data.get("ThrottleStatus") not in (0, None):
        return None

    result = data.get("IfExistsResult")
    if result in _EXISTS_CODES:
        return True
    if result == 1:
        return False
    return None


PATTERN_SPECIFICITY: dict[str, int] = {
    "first.last": 3, "firstlast": 3, "first_last": 3, "first-last": 3,
    "last.first": 3, "lastfirst": 3, "last_first": 3,
    "flast": 2, "f.last": 2, "firstl": 2, "first.l": 2, "lastf": 2,
    "first": 1, "last": 1, "fl": 1,
}


def _generate_patterns(first: str, last: str, domain: str) -> list[tuple[str, str]]:
    """Generate (email, pattern_label) pairs for common corporate formats."""
    f = first.lower().strip()
    l = last.lower().strip()
    if not f:
        return []
    candidates = [
        (f"{f}.{l}", "first.last"),
        (f"{f}{l}", "firstlast"),
        (f"{f}_{l}", "first_last"),
        (f"{f}-{l}", "first-last"),
        (f"{f[0]}{l}", "flast"),
        (f"{f}{l[0]}", "firstl"),
        (f"{f[0]}.{l}", "f.last"),
        (f"{f}.{l[0]}", "first.l"),
        (f"{f}", "first"),
        (f"{l}.{f}", "last.first"),
        (f"{l}{f}", "lastfirst"),
        (f"{l}{f[0]}", "lastf"),
        (f"{l}", "last"),
        (f"{l}_{f}", "last_first"),
        (f"{f[0]}{l[0]}", "fl"),
    ] if l else [
        (f"{f}", "first"),
    ]
    seen = set()
    unique = []
    for local, label in candidates:
        if local not in seen:
            seen.add(local)
            unique.append((f"{local}@{domain}", label))
    return unique


@dataclass
class PatternHit:
    email: str
    pattern: str
    exists: bool | None


def m365_pattern_search(
    first: str, last: str, domain: str, timeout: int = 10
) -> list[PatternHit]:
    """Try all common email patterns against Microsoft's GetCredentialType.

    Returns a list of PatternHit for every candidate, with exists=True/False/None.
    Reuses a single httpx client for speed.
    """
    candidates = _generate_patterns(first, last, domain)
    results: list[PatternHit] = []
    try:
        with httpx.Client(timeout=timeout) as client:
            for email, label in candidates:
                try:
                    resp = client.post(_ENDPOINT, json={"Username": email})
                    if resp.status_code != 200:
                        results.append(PatternHit(email, label, None))
                        continue
                    data = resp.json()
                    if data.get("ThrottleStatus") not in (0, None):
                        results.append(PatternHit(email, label, None))
                        continue
                    code = data.get("IfExistsResult")
                    if code in _EXISTS_CODES:
                        results.append(PatternHit(email, label, True))
                    elif code == 1:
                        results.append(PatternHit(email, label, False))
                    else:
                        results.append(PatternHit(email, label, None))
                except Exception:
                    results.append(PatternHit(email, label, None))
    except Exception:
        pass
    return results
