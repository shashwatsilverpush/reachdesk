"""Email pattern generation for scope/find-email.

Standalone module — only needs stdlib. No SMTP, DNS, or external packages.
This allows pattern generation on Shipyard where email_verifier isn't installed.
"""

from __future__ import annotations

PATTERN_SPECIFICITY: dict[str, int] = {
    # 4 = externally verified (Apollo, etc.)
    "apollo": 4,
    # 3 = full first+last (unique to this person)
    "first.last": 3, "firstlast": 3, "first_last": 3, "first-last": 3,
    "last.first": 3, "lastfirst": 3, "last_first": 3,
    # 3 = includes middle name (even more specific)
    "first.middle.last": 3, "firstmiddlelast": 3, "first.m.last": 3,
    "fmlast": 3, "f.m.last": 3, "fm.last": 3, "fm_last": 3, "fm-last": 3,
    "first.middle": 3, "firstmiddle": 3, "first_middle_last": 3,
    "first-middle-last": 3, "firstm": 3,
    "last.fm": 3, "lastfm": 3, "fmiddlel": 3,
    # 2 = partial name
    "flast": 2, "f.last": 2, "f_last": 2, "f-last": 2,
    "firstl": 2, "first.l": 2, "lastf": 2,
    "f.middle": 2, "fmiddle": 2, "middle.last": 2, "middlelast": 2,
    "middlef": 2, "f.m": 2,
    # 1 = single name part or brute-force middle-initial guess
    "first": 1, "last": 1, "middle": 1, "fl": 1, "fml": 1,
    **{f"f?last:{c}": 1 for c in "abcdefghijklmnopqrstuvwxyz"},
    # 0 = single initial
    "f": 0, "l": 0, "m": 0,
}


def generate_patterns(first: str, last: str, domain: str) -> list[tuple[str, str]]:
    """Generate (email, pattern_label) pairs for common corporate formats."""
    raw_first = first.lower().strip()
    raw_last = last.lower().strip()
    if not raw_first:
        return []

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

    candidates: list[tuple[str, str]] = []
    if l:
        candidates += [
            (f"{f}.{l}", "first.last"),
            (f"{f}{l}", "firstlast"),
            (f"{f}_{l}", "first_last"),
            (f"{f}-{l}", "first-last"),
            (f"{f[0]}{l}", "flast"),
            (f"{f[0]}_{l}", "f_last"),
            (f"{f[0]}-{l}", "f-last"),
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

    if mid and l:
        candidates += [
            (f"{f}.{mid}.{l}", "first.middle.last"),
            (f"{f}{mid}{l}", "firstmiddlelast"),
            (f"{f}_{mid}_{l}", "first_middle_last"),
            (f"{f}-{mid}-{l}", "first-middle-last"),
            (f"{f}.{mid[0]}.{l}", "first.m.last"),
            (f"{f[0]}{mid[0]}{l}", "fmlast"),
            (f"{f[0]}.{mid[0]}.{l}", "f.m.last"),
            (f"{f[0]}{mid[0]}.{l}", "fm.last"),
            (f"{f[0]}{mid[0]}_{l}", "fm_last"),
            (f"{f[0]}{mid[0]}-{l}", "fm-last"),
            (f"{l}.{f[0]}{mid[0]}", "last.fm"),
            (f"{l}{f[0]}{mid[0]}", "lastfm"),
            (f"{f[0]}{mid}{l[0]}", "fmiddlel"),
            (f"{f}.{mid}", "first.middle"),
            (f"{f}{mid}", "firstmiddle"),
            (f"{f}{mid[0]}", "firstm"),
            (f"{f[0]}.{mid}", "f.middle"),
            (f"{f[0]}{mid}", "fmiddle"),
            (f"{mid}.{l}", "middle.last"),
            (f"{mid}{l}", "middlelast"),
            (f"{mid}{f[0]}", "middlef"),
            (f"{f[0]}.{mid[0]}", "f.m"),
            (f"{f[0]}{mid[0]}{l[0]}", "fml"),
            (f"{mid}", "middle"),
            (f"{mid[0]}", "m"),
        ]
    elif mid and not l:
        candidates += [
            (f"{f}.{mid}", "first.middle"),
            (f"{f}{mid}", "firstmiddle"),
            (f"{f[0]}{mid}", "fmiddle"),
            (f"{f[0]}.{mid}", "f.middle"),
            (f"{f}{mid[0]}", "firstm"),
            (f"{mid}", "middle"),
        ]

    if not mid and l:
        for c in "abcdefghijklmnopqrstuvwxyz":
            candidates.append((f"{f[0]}{c}{l}", f"f?last:{c}"))

    seen: set[str] = set()
    unique: list[tuple[str, str]] = []
    for local, label in candidates:
        if local not in seen:
            seen.add(local)
            unique.append((f"{local}@{domain}", label))
    return unique
