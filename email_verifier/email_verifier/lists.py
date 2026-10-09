"""Reference lists for email classification: role accounts, disposable/free/popular domains.

Each list can be overridden at runtime via a TOML config file (--config).
"""

from __future__ import annotations

import sys
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib
else:
    try:
        import tomli as tomllib  # type: ignore[no-redef]
    except ImportError:
        tomllib = None  # type: ignore[assignment]

ROLE_ACCOUNTS: set[str] = {
    "info", "admin", "administrator", "support", "sales", "contact", "help",
    "billing", "careers", "jobs", "postmaster", "hostmaster", "webmaster",
    "abuse", "security", "noreply", "no-reply", "donotreply", "marketing",
    "hr", "legal", "press", "team", "office", "mail", "hello",
}

DISPOSABLE_DOMAINS: set[str] = {
    "mailinator.com", "10minutemail.com", "guerrillamail.com", "temp-mail.org",
    "tempmail.com", "yopmail.com", "throwawaymail.com", "trashmail.com",
    "sharklasers.com", "getnada.com", "dispostable.com", "maildrop.cc",
    "mintemail.com", "tempinbox.com", "fakeinbox.com", "mytemp.email",
    "discard.email", "spamgourmet.com", "burnermail.io", "moakt.com",
}

POPULAR_DOMAINS: set[str] = {
    "gmail.com", "googlemail.com", "yahoo.com", "ymail.com", "hotmail.com",
    "outlook.com", "live.com", "msn.com", "aol.com", "icloud.com", "me.com",
    "mac.com", "proton.me", "protonmail.com", "zoho.com", "gmx.com", "gmx.de",
    "mail.com", "fastmail.com", "yandex.com", "yandex.ru", "qq.com", "163.com",
    "126.com", "sina.com", "rediffmail.com", "comcast.net", "verizon.net",
    "att.net", "sbcglobal.net", "earthlink.net",
}

FREE_PROVIDERS: set[str] = {
    "gmail.com", "googlemail.com", "yahoo.com", "ymail.com", "aol.com",
    "outlook.com", "hotmail.com", "live.com", "msn.com", "icloud.com",
    "me.com", "mac.com", "proton.me", "protonmail.com", "gmx.com", "gmx.de",
    "mail.com", "yandex.com", "yandex.ru", "zoho.com", "qq.com", "163.com",
}


def load_overrides(path: str) -> dict[str, Any]:
    """Load list/profile overrides from a TOML file.

    Supported keys: role_accounts, disposable_domains, popular_domains,
    free_providers, provider_profiles.
    """
    if tomllib is None:
        raise SystemExit(
            "TOML config requires Python >= 3.11 or the 'tomli' package. "
            "Install tomli (`pip install tomli`) or upgrade Python."
        )
    with open(path, "rb") as fh:
        return tomllib.load(fh)


def get_role_accounts(overrides: dict[str, Any] | None = None) -> set[str]:
    """Return the set of role-account local parts, with optional overrides."""
    if overrides and "role_accounts" in overrides:
        return set(overrides["role_accounts"])
    return ROLE_ACCOUNTS


def get_disposable_domains(overrides: dict[str, Any] | None = None) -> set[str]:
    """Return the set of disposable domains, with optional overrides."""
    if overrides and "disposable_domains" in overrides:
        return set(overrides["disposable_domains"])
    return DISPOSABLE_DOMAINS


def get_popular_domains(overrides: dict[str, Any] | None = None) -> set[str]:
    """Return the set of popular domains, with optional overrides."""
    if overrides and "popular_domains" in overrides:
        return set(overrides["popular_domains"])
    return POPULAR_DOMAINS


def get_free_providers(overrides: dict[str, Any] | None = None) -> set[str]:
    """Return the set of free provider domains, with optional overrides."""
    if overrides and "free_providers" in overrides:
        return set(overrides["free_providers"])
    return FREE_PROVIDERS
