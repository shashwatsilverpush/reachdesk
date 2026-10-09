"""Provider profiles and signup-probe interface (Stage 5).

Provider profiles encode known SMTP behaviors of major email providers.
These behaviors drift over time — re-test periodically and override at
runtime via --config.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ProviderProfile:
    """Known SMTP behavior for a mail provider family."""

    family: str
    smtp_reveals: bool
    max_rcpt_per_conn: int = 1
    greylist_prone: bool = False
    signup_probe_possible: bool = False
    notes: str = ""


PROVIDER_PROFILES: dict[str, ProviderProfile] = {
    "gmail.com":      ProviderProfile("google",    smtp_reveals=False,
                                       signup_probe_possible=True,
                                       notes="Accepts any local part; dots and +tags ignored."),
    "googlemail.com": ProviderProfile("google",    smtp_reveals=False,
                                       signup_probe_possible=True, notes="Alias of gmail.com."),
    "outlook.com":    ProviderProfile("microsoft", smtp_reveals=False, greylist_prone=True,
                                       notes="Throttles probes; may 550 a few then 421 everything."),
    "hotmail.com":    ProviderProfile("microsoft", smtp_reveals=False, greylist_prone=True),
    "live.com":       ProviderProfile("microsoft", smtp_reveals=False, greylist_prone=True),
    "msn.com":        ProviderProfile("microsoft", smtp_reveals=False, greylist_prone=True),
    "yahoo.com":      ProviderProfile("yahoo",     smtp_reveals=False, greylist_prone=True,
                                       signup_probe_possible=True, notes="4xx after 1-3 probes."),
    "aol.com":        ProviderProfile("yahoo",     smtp_reveals=False, greylist_prone=True),
    "icloud.com":     ProviderProfile("apple",     smtp_reveals=True,
                                       notes="Reveals nonexistent but drops the connection fast; one RCPT per connection."),
    "me.com":         ProviderProfile("apple",     smtp_reveals=True),
    "mac.com":        ProviderProfile("apple",     smtp_reveals=True),
    "proton.me":      ProviderProfile("proton",    smtp_reveals=False, notes="Verify current behavior."),
    "protonmail.com": ProviderProfile("proton",    smtp_reveals=False),
    "zoho.com":       ProviderProfile("zoho",      smtp_reveals=True),
    "gmx.com":        ProviderProfile("united",    smtp_reveals=True),
    "gmx.de":         ProviderProfile("united",    smtp_reveals=True),
    "mail.com":       ProviderProfile("united",    smtp_reveals=True),
    "yandex.com":     ProviderProfile("yandex",    smtp_reveals=True),
    "yandex.ru":      ProviderProfile("yandex",    smtp_reveals=True),
    "qq.com":         ProviderProfile("tencent",   smtp_reveals=True, greylist_prone=True),
    "163.com":        ProviderProfile("netease",   smtp_reveals=True),
}

DEFAULT_PROFILE = ProviderProfile("corporate", smtp_reveals=True)


def get_provider_profile(
    domain: str, overrides: dict[str, Any] | None = None
) -> ProviderProfile:
    """Look up the provider profile for a domain.

    Falls back to DEFAULT_PROFILE for unknown domains.
    """
    if overrides and "provider_profiles" in overrides:
        custom = overrides["provider_profiles"]
        if domain in custom:
            p = custom[domain]
            return ProviderProfile(
                family=p.get("family", "custom"),
                smtp_reveals=p.get("smtp_reveals", True),
                max_rcpt_per_conn=p.get("max_rcpt_per_conn", 1),
                greylist_prone=p.get("greylist_prone", False),
                signup_probe_possible=p.get("signup_probe_possible", False),
                notes=p.get("notes", ""),
            )
    return PROVIDER_PROFILES.get(domain, DEFAULT_PROFILE)


class SignupProbe(abc.ABC):
    """Abstract interface for provider signup-page probes.

    A signup probe checks mailbox existence by hitting a provider's
    account-creation form.  Concrete implementations are NOT shipped
    because automating provider signup forms violates provider ToS,
    hits CAPTCHAs, and is fragile.  This class exists only as a hook
    for users who build their own probes for domains they control.
    """

    @abc.abstractmethod
    def check(self, local: str, domain: str) -> bool | None:
        """True = mailbox almost certainly exists. None = cannot determine."""


class DisabledSignupProbe(SignupProbe):
    """Stub that always returns None.

    Signup probes are disabled by default because:
    - Automating provider signup forms violates their Terms of Service.
    - Providers deploy CAPTCHAs that break automated probes quickly.
    - HTTP scraping is fragile and provider-specific.

    Pass a concrete SignupProbe subclass via --config to enable.
    """

    def check(self, local: str, domain: str) -> bool | None:
        """Always returns None — signup probing is disabled."""
        return None
