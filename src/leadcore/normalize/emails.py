"""Email canonicalisation and splitting."""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import unquote

_VALID = re.compile(r"^[a-z0-9!#$%&'*+/=?^_`{|}~.\-]+@[a-z0-9\-]+(\.[a-z0-9\-]+)+$")
_SPLIT = re.compile(r"[,;/\s]+(?=[^,;/\s]*@)")
_TRAILING_DIGITS = re.compile(r"\d+$")


@dataclass(frozen=True)
class ParsedEmail:
    email: str          # canonical: lowercase, trimmed
    local: str          # as held, lowercased
    domain: str
    match_local: str    # local with +tag and trailing digits removed, used for matching
    numeric_suffix: str | None


def split_cell(raw: object) -> list[str]:
    """A cell may hold several addresses ("a@x.com; b@x.com"). Return each raw piece."""
    if raw is None:
        return []
    s = str(raw).strip()
    if not s or s.lower() == "nan":
        return []
    if s.count("@") <= 1:
        return [s]
    return [p for p in _SPLIT.split(s) if p.strip()]


def canonicalise(raw: str) -> str | None:
    """Return a canonical address, or None if the value is not an address.

    Strips ``mailto:``, angle brackets, surrounding punctuation and *internal*
    whitespace (the sources contain ``priscilla.ramos@tvn .cl``).
    """
    s = unquote(str(raw)).strip().lower()
    if s.startswith("mailto:"):
        s = s[7:]
    s = s.strip("<>()[]\"' ,;:")
    s = re.sub(r"\s+", "", s)
    s = s.rstrip(".")
    if not _VALID.match(s):
        return None
    local, domain = s.rsplit("@", 1)
    if not local or local.startswith(".") or local.endswith(".") or ".." in local:
        return None
    return s


def parse(email: str) -> ParsedEmail:
    local, domain = email.rsplit("@", 1)
    base = local.split("+", 1)[0]
    m = _TRAILING_DIGITS.search(base)
    suffix = m.group(0) if m and m.start() > 0 else None
    match_local = base[: m.start()] if suffix else base
    return ParsedEmail(email, local, domain, match_local.strip(".-_"), suffix)


def registrable_domain(value: object) -> str | None:
    """Best-effort domain from a free-text company/domain cell (``www.Foo.com/`` -> foo.com)."""
    if value is None:
        return None
    s = str(value).strip().lower()
    if not s or s == "nan":
        return None
    s = re.sub(r"^[a-z]+://", "", s)
    s = s.split("/")[0].split("?")[0]
    if s.startswith("www."):
        s = s[4:]
    return s if re.fullmatch(r"[a-z0-9\-]+(\.[a-z0-9\-]+)+", s) else None
