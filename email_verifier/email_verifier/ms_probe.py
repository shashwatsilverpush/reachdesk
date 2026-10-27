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
    # 3 = full first+last (unique to this person)
    "first.last": 3, "firstlast": 3, "first_last": 3, "first-last": 3,
    "last.first": 3, "lastfirst": 3, "last_first": 3,
    # 3 = includes middle name (even more specific)
    "first.middle.last": 3, "firstmiddlelast": 3, "first.m.last": 3,
    "fmlast": 3, "f.m.last": 3,
    "first.middle": 3, "firstmiddle": 3, "first_middle_last": 3,
    "first-middle-last": 3, "firstm": 3,
    # 2 = partial name
    "flast": 2, "f.last": 2, "firstl": 2, "first.l": 2, "lastf": 2,
    "f.middle": 2, "fmiddle": 2, "middle.last": 2, "middlelast": 2,
    "middlef": 2, "f.m": 2,
    # 1 = single name part
    "first": 1, "last": 1, "middle": 1, "fl": 1, "fml": 1,
    # 0 = single initial
    "f": 0, "l": 0, "m": 0,
}


def _generate_patterns(first: str, last: str, domain: str) -> list[tuple[str, str]]:
    """Generate (email, pattern_label) pairs for common corporate formats.

    Handles multi-part names: if *first* contains spaces (e.g. "Mansi Kumar")
    the extra parts become the middle name. Same for multi-word *last*.
    """
    raw_first = first.lower().strip()
    raw_last = last.lower().strip()
    if not raw_first:
        return []

    # Split out middle name from multi-word first or last
    first_parts = raw_first.split()
    last_parts = raw_last.split()

    f = first_parts[0]
    if len(first_parts) > 1:
        mid = first_parts[1]
        l = raw_last
    elif len(last_parts) > 1:
        mid = last_parts[0]
        l = last_parts[-1]
    else:
        mid = ""
        l = raw_last

    # Base two-part patterns (first + last)
    candidates: list[tuple[str, str]] = []
    if l:
        candidates += [
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
            (f"{f[0]}", "f"),
            (f"{l[0]}", "l"),
        ]
    else:
        candidates.append((f"{f}", "first"))

    # Middle-name patterns (only when a middle name exists)
    if mid and l:
        candidates += [
            # Full three-part
            (f"{f}.{mid}.{l}", "first.middle.last"),
            (f"{f}{mid}{l}", "firstmiddlelast"),
            (f"{f}_{mid}_{l}", "first_middle_last"),
            (f"{f}-{mid}-{l}", "first-middle-last"),
            # First + middle initial + last
            (f"{f}.{mid[0]}.{l}", "first.m.last"),
            (f"{f[0]}{mid[0]}{l}", "fmlast"),
            (f"{f[0]}.{mid[0]}.{l}", "f.m.last"),
            # First + middle (no last)
            (f"{f}.{mid}", "first.middle"),
            (f"{f}{mid}", "firstmiddle"),
            (f"{f}{mid[0]}", "firstm"),
            # First initial + middle
            (f"{f[0]}.{mid}", "f.middle"),
            (f"{f[0]}{mid}", "fmiddle"),
            # Middle + last
            (f"{mid}.{l}", "middle.last"),
            (f"{mid}{l}", "middlelast"),
            # Middle + first initial
            (f"{mid}{f[0]}", "middlef"),
            # Initials
            (f"{f[0]}.{mid[0]}", "f.m"),
            (f"{f[0]}{mid[0]}{l[0]}", "fml"),
            # Middle standalone
            (f"{mid}", "middle"),
            (f"{mid[0]}", "m"),
        ]
    elif mid and not l:
        # Middle but no last — treat middle as last
        candidates += [
            (f"{f}.{mid}", "first.middle"),
            (f"{f}{mid}", "firstmiddle"),
            (f"{f[0]}{mid}", "fmiddle"),
            (f"{f[0]}.{mid}", "f.middle"),
            (f"{f}{mid[0]}", "firstm"),
            (f"{mid}", "middle"),
        ]

    seen: set[str] = set()
    unique: list[tuple[str, str]] = []
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
