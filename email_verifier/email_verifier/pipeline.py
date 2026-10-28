"""Stage 6 — Orchestrator: runs all stages and applies the verdict mapping.

Verdict mapping (applied in order):

  Condition                                     Verdict   Reason
  ─────────────────────────────────────────────  ───────   ──────────────────
  Syntax failed                                  invalid   syntax
  No DNS for domain                              invalid   no_dns
  No MX / Null MX / no A fallback                invalid   no_mx
  SMTP 550/551/553                               invalid   smtp_rejected_<code>
  SMTP 552                                       valid     mailbox_full
  4xx after all retries                          unknown   greylisted
  All MX connections failed                      unknown   unreachable
  250/251, catch_all == False                    valid     smtp_confirmed
  250/251, catch_all == True, known free prov.   unknown   hidden_provider
  250/251, catch_all == True, other domain       risky     catch_all_domain
  Disposable domain flag present                 capped    disposable
  Role account                                   flag      role
"""

from __future__ import annotations

from typing import Any

import dns.resolver

from email_verifier.dnsx import clear_mx_cache, domain_exists, get_mx_hosts_cached
from email_verifier.lists import get_free_providers
from email_verifier.models import Evidence, Result
from email_verifier.ms_probe import check_m365_account, is_m365_mx
from email_verifier.providers import (
    DisabledSignupProbe,
    SignupProbe,
    get_provider_profile,
)
from email_verifier.smtp_probe import smtp_check
from email_verifier.syntax import check_syntax


def verify_email(
    email: str,
    *,
    sender: str = "",
    helo: str | None = None,
    timeout: int = 15,
    port: int = 25,
    retries: int = 1,
    delay: float = 5.0,
    detect_catch_all: bool = True,
    dns_resolver: Any | None = None,
    overrides: dict[str, Any] | None = None,
    signup_probe: SignupProbe | None = None,
) -> Result:
    """Run the full 6-stage verification pipeline for a single email address.

    Args:
        email:            The address to verify.
        sender:           MAIL FROM address for SMTP probes.
        helo:             EHLO hostname (defaults to local FQDN).
        timeout:          SMTP connection timeout in seconds.
        port:             SMTP port (default 25; override for tests).
        retries:          Greylist retry attempts.
        delay:            Seconds between retry attempts.
        detect_catch_all: Whether to run catch-all detection.
        dns_resolver:     Injectable DNS resolver (for tests).
        overrides:        TOML config overrides dict.
        signup_probe:     Optional signup probe implementation.

    Returns:
        A fully populated Result.
    """
    # Stage 1 — Syntax
    syn = check_syntax(email, overrides=overrides)
    if not syn.valid:
        return Result(
            email=email,
            verdict="invalid",
            reason="syntax",
            flags=syn.flags,
            suggestion=syn.suggestion,
        )

    email_normalized = syn.email
    local = syn.local
    domain = syn.domain
    flags = list(syn.flags)
    suggestion = syn.suggestion

    # Stage 2 — DNS
    try:
        mx_hosts = get_mx_hosts_cached(domain, resolver=dns_resolver)
    except dns.resolver.NXDOMAIN:
        return Result(
            email=email_normalized,
            verdict="invalid",
            reason="no_dns",
            flags=flags,
            suggestion=suggestion,
        )

    if not mx_hosts:
        if not domain_exists(domain, resolver=dns_resolver):
            reason = "no_dns"
        else:
            reason = "no_mx"
        return Result(
            email=email_normalized,
            verdict="invalid",
            reason=reason,
            flags=flags,
            suggestion=suggestion,
        )

    evidence = Evidence(mx=list(mx_hosts))

    # Stage 5 — Provider profile (looked up before SMTP to inform probe behavior)
    profile = get_provider_profile(domain, overrides)

    # Stage 3+4 — SMTP probe + catch-all
    smtp_result = smtp_check(
        email=email_normalized,
        domain=domain,
        mx_hosts=mx_hosts,
        sender=sender,
        helo=helo,
        timeout=timeout,
        port=port,
        retries=retries,
        delay=delay,
        detect_catch_all=detect_catch_all,
    )

    evidence.rcpt_code = smtp_result.code
    evidence.rcpt_message = smtp_result.message
    evidence.catch_all = smtp_result.catch_all
    evidence.greylist_retries = smtp_result.greylist_retries
    evidence.notes = list(smtp_result.notes)

    # Stage 5a — Microsoft 365 account probe (resolves catch-all ambiguity)
    ms_probe_result = None
    if smtp_result.catch_all is True and any(is_m365_mx(h) for h in mx_hosts):
        ms_probe_result = check_m365_account(email_normalized, timeout=timeout)
        if ms_probe_result is True:
            evidence.notes.append("m365_account_exists")
        elif ms_probe_result is False:
            evidence.notes.append("m365_account_not_found")
        else:
            evidence.notes.append("m365_probe_failed")

    # Stage 5b — Signup probe (hook only, disabled by default)
    if signup_probe and not isinstance(signup_probe, DisabledSignupProbe):
        probe_result = signup_probe.check(local, domain)
        if probe_result is True:
            evidence.notes.append("signup_probe_confirmed")
        elif probe_result is None:
            evidence.notes.append("signup_probe_inconclusive")

    # Stage 6 — Verdict mapping
    verdict, reason = _map_verdict(smtp_result, domain, flags, evidence, overrides, ms_probe_result)

    return Result(
        email=email_normalized,
        verdict=verdict,
        reason=reason,
        flags=flags,
        suggestion=suggestion,
        evidence=evidence,
    )


def _map_verdict(
    smtp_result: Any,
    domain: str,
    flags: list[str],
    evidence: Evidence,
    overrides: dict[str, Any] | None,
    ms_probe_result: bool | None = None,
) -> tuple[str, str]:
    """Apply the verdict mapping rules in order."""
    code = smtp_result.code
    status = smtp_result.status
    catch_all = smtp_result.catch_all

    # SMTP rejected
    if status == "invalid":
        verdict = "invalid"
        reason = f"smtp_rejected_{code}"
        return _apply_flags(verdict, reason, flags, domain, overrides)

    # Mailbox full (552) — exists
    if status == "valid" and code == 552:
        verdict = "valid"
        reason = "mailbox_full"
        return _apply_flags(verdict, reason, flags, domain, overrides)

    # Unknown — greylisted or unreachable
    if status == "unknown":
        if smtp_result.greylist_retries > 0 and any(
            note for note in smtp_result.notes
        ):
            reason = "greylisted"
        elif code == 0 and not smtp_result.notes:
            reason = "unreachable"
        else:
            reason = "greylisted"
        return _apply_flags("unknown", reason, flags, domain, overrides)

    # Valid (250/251) — check catch-all
    if status == "valid":
        if catch_all is True:
            # M365 probe can override catch-all ambiguity
            if ms_probe_result is True:
                return _apply_flags("valid", "m365_confirmed", flags, domain, overrides)
            if ms_probe_result is False:
                return _apply_flags("invalid", "m365_not_found", flags, domain, overrides)

            free = get_free_providers(overrides)
            if domain in free:
                verdict = "unknown"
                reason = "hidden_provider"
            else:
                verdict = "risky"
                reason = "catch_all_domain"
            return _apply_flags(verdict, reason, flags, domain, overrides)
        elif catch_all is None:
            evidence.notes.append("catch_all_probe_failed")
            verdict = "valid"
            reason = "smtp_confirmed"
            return _apply_flags(verdict, reason, flags, domain, overrides)
        else:
            verdict = "valid"
            reason = "smtp_confirmed"
            return _apply_flags(verdict, reason, flags, domain, overrides)

    return _apply_flags("unknown", "unverifiable", flags, domain, overrides)


def _apply_flags(
    verdict: str,
    reason: str,
    flags: list[str],
    domain: str,
    overrides: dict[str, Any] | None,
) -> tuple[str, str]:
    """Apply flag-based adjustments to the verdict.

    - Disposable domain: cap verdict at 'risky'.
    - Role account: flag only, verdict unchanged.
    """
    if "disposable" in flags:
        if verdict == "valid":
            verdict = "risky"
            reason = "disposable"

    return verdict, reason
