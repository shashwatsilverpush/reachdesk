"""Stages 3 & 4 — SMTP RCPT TO probe and catch-all detection.

Preserves baseline behavior:
  {250, 251} → valid
  {550, 551, 553} → invalid (rejected)
  552 → valid (mailbox full — it exists)
  252 and all 4xx → unknown
"""

from __future__ import annotations

import random
import smtplib
import socket
import string
import time
from dataclasses import dataclass, field
from functools import lru_cache


@lru_cache(maxsize=1)
def _cached_fqdn() -> str:
    """Cache the local FQDN to avoid repeated slow DNS lookups."""
    return socket.getfqdn()


def classify(code: int) -> str:
    """Map an SMTP response code to a status string.

    Returns:
        ``"valid"``   for 250, 251, 552 (mailbox full proves existence).
        ``"invalid"`` for 550, 551, 553.
        ``"unknown"`` for everything else (252, 4xx, errors).
    """
    if code in (250, 251):
        return "valid"
    if code in (550, 551, 553):
        return "invalid"
    if code == 552:
        return "valid"
    return "unknown"


def smtp_rcpt(
    email: str,
    mx: str,
    sender: str,
    helo: str | None = None,
    timeout: int = 15,
    port: int = 25,
) -> tuple[int, str]:
    """Perform a single SMTP RCPT TO probe against one MX host.

    Opens a fresh connection, sends EHLO → MAIL FROM → RCPT TO, then
    closes.  One RCPT per connection (polite behavior).

    Returns:
        ``(smtp_code, message_string)``  where code is 0 on connection
        failure.
    """
    helo = helo or _cached_fqdn()
    smtp = smtplib.SMTP(timeout=timeout, local_hostname=helo)
    try:
        smtp.connect(mx, port)
        smtp.ehlo(helo)
        smtp.mail(sender)
        code, msg = smtp.rcpt(email)
        return code, msg.decode(errors="replace")
    except smtplib.SMTPServerDisconnected as exc:
        return 421, f"server disconnected: {exc}"
    except (smtplib.SMTPException, OSError) as exc:
        return 0, str(exc)
    finally:
        try:
            smtp.quit()
        except Exception:
            try:
                smtp.close()
            except Exception:
                pass


def is_catch_all(
    domain: str,
    mx: str,
    sender: str,
    helo: str | None = None,
    timeout: int = 15,
    port: int = 25,
) -> bool | None:
    """Probe whether *domain* accepts any local part (catch-all).

    Sends a RCPT TO for a random ``probe-<16 chars>@domain`` address.

    Returns:
        ``True``  if the random address was accepted (250/251) — catch-all.
        ``False`` if it was rejected (550/551/553) — not catch-all.
        ``None``  if the probe errored or gave an ambiguous response.
    """
    random_local = "probe-" + "".join(
        random.choices(string.ascii_lowercase + string.digits, k=16)
    )
    code, _ = smtp_rcpt(f"{random_local}@{domain}", mx, sender, helo, timeout, port)
    if code in (250, 251):
        return True
    if code in (550, 551, 553):
        return False
    return None


@dataclass
class SmtpCheckResult:
    """Intermediate result from the SMTP probe stage."""

    status: str  # "valid", "invalid", "unknown"
    code: int = 0
    message: str = ""
    mx: str | None = None
    catch_all: bool | None = None
    greylist_retries: int = 0
    notes: list[str] = field(default_factory=list)


def smtp_check(
    email: str,
    domain: str,
    mx_hosts: list[str],
    sender: str = "",
    helo: str | None = None,
    timeout: int = 15,
    port: int = 25,
    retries: int = 1,
    delay: float = 5.0,
    detect_catch_all: bool = True,
) -> SmtpCheckResult:
    """Run SMTP probes across MX hosts with retry logic.

    Iterates MX hosts in preference order.  On an unknown/4xx result,
    moves to the next MX.  If all MX hosts return unknown, waits
    *delay* seconds and retries up to *retries* times.

    This function preserves the baseline ``check_email`` behavior with
    the addition of configurable retries and port.
    """
    if not sender:
        sender = f"verify@{domain}"
    all_notes: list[str] = []
    actual_retries = 0

    for attempt in range(retries + 1):
        notes: list[str] = []
        for mx in mx_hosts:
            code, msg = smtp_rcpt(email, mx, sender, helo, timeout, port)
            status = classify(code)

            if status == "valid":
                catch_all: bool | None = None
                if detect_catch_all:
                    catch_all = is_catch_all(domain, mx, sender, helo, timeout, port)

                return SmtpCheckResult(
                    status="valid",
                    code=code,
                    message=msg,
                    mx=mx,
                    catch_all=catch_all,
                    greylist_retries=actual_retries,
                    notes=notes,
                )

            if status == "invalid":
                return SmtpCheckResult(
                    status="invalid",
                    code=code,
                    message=msg,
                    mx=mx,
                    greylist_retries=actual_retries,
                    notes=notes,
                )

            notes.append(f"{mx}: {code} {msg}")

        all_notes.extend(notes)

        if attempt < retries:
            time.sleep(delay)
            actual_retries += 1

    return SmtpCheckResult(
        status="unknown",
        code=0,
        message="unverifiable | " + " | ".join(all_notes),
        greylist_retries=actual_retries,
        notes=all_notes,
    )
