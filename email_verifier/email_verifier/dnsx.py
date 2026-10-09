"""Stage 2 — DNS MX resolution with injectable resolver and process-lifetime cache."""

from __future__ import annotations

from functools import lru_cache
from typing import Any, Protocol

import dns.name
import dns.resolver


class Resolver(Protocol):
    """Minimal resolver interface (compatible with dns.resolver.Resolver)."""

    def resolve(self, name: str, rdtype: str, lifetime: float | None = ...) -> Any: ...


_default_resolver: dns.resolver.Resolver | None = None


def _get_default_resolver() -> dns.resolver.Resolver:
    """Lazily create a default dnspython resolver."""
    global _default_resolver
    if _default_resolver is None:
        _default_resolver = dns.resolver.Resolver()
    return _default_resolver


def get_mx_hosts(
    domain: str,
    resolver: Any | None = None,
    lifetime: float = 10.0,
) -> list[str]:
    """Resolve MX records for *domain*, sorted by preference (lowest first).

    Falls back to the domain itself when MX records are absent but an
    A/AAAA record exists (implicit MX per RFC 5321 §5).

    Treats Null MX (RFC 7505: a single MX with exchange ``"."``) as
    "no mail server" and returns an empty list.

    Args:
        domain:   The email domain to look up.
        resolver: An object with a ``.resolve(name, rdtype, lifetime)``
                  method.  Defaults to a real dnspython resolver.
        lifetime: DNS query timeout in seconds.

    Returns:
        A list of MX hostnames sorted by preference, or an empty list.

    Raises:
        dns.resolver.NXDOMAIN: The domain does not exist in DNS at all.
    """
    r = resolver or _get_default_resolver()
    try:
        answers = r.resolve(domain, "MX", lifetime=lifetime)
        hosts = [
            (rdata.preference, str(rdata.exchange).rstrip("."))
            for rdata in answers
        ]
        # Null MX check (RFC 7505): single record with exchange "." (or empty after rstrip)
        if len(hosts) == 1 and hosts[0][1] in ("", "."):
            return []
        hosts.sort(key=lambda x: x[0])
        return [h for _, h in hosts]
    except dns.resolver.NoAnswer:
        pass
    except dns.resolver.NXDOMAIN:
        raise

    # No MX records — fall back to A/AAAA
    return _a_fallback(domain, r, lifetime)


def domain_exists(
    domain: str,
    resolver: Any | None = None,
    lifetime: float = 10.0,
) -> bool:
    """Check whether the domain has any DNS presence (A or AAAA)."""
    r = resolver or _get_default_resolver()
    for rdtype in ("A", "AAAA"):
        try:
            r.resolve(domain, rdtype, lifetime=lifetime)
            return True
        except (dns.resolver.NoAnswer, dns.resolver.NXDOMAIN, dns.resolver.NoNameservers):
            continue
        except Exception:
            continue
    return False


def _a_fallback(
    domain: str,
    resolver: Any,
    lifetime: float,
) -> list[str]:
    """Return [domain] if the domain has an A/AAAA record, else []."""
    for rdtype in ("A", "AAAA"):
        try:
            resolver.resolve(domain, rdtype, lifetime=lifetime)
            return [domain]
        except (dns.resolver.NoAnswer, dns.resolver.NXDOMAIN, dns.resolver.NoNameservers):
            continue
        except Exception:
            continue
    return []


# Process-lifetime MX cache keyed on domain.
_mx_cache: dict[str, list[str]] = {}


def get_mx_hosts_cached(
    domain: str,
    resolver: Any | None = None,
    lifetime: float = 10.0,
) -> list[str]:
    """Like get_mx_hosts but caches results for the process lifetime."""
    if domain not in _mx_cache:
        _mx_cache[domain] = get_mx_hosts(domain, resolver=resolver, lifetime=lifetime)
    return _mx_cache[domain]


def clear_mx_cache() -> None:
    """Clear the MX cache (useful in tests)."""
    _mx_cache.clear()
