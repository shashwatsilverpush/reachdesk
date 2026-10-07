"""Import results from an external email-verification service, and export candidates for one.

This module never checks an address itself. It takes a results file produced
by a third-party service (ZeroBounce, NeverBounce, MillionVerifier, Bouncer,
Apollo, ...) and records each result, and it exports the addresses that still
need checking as a CSV those services accept.

Column names differ by service, so the import maps them. A handful of known
layouts are built in (``SERVICE_PRESETS``); for anything else, pass an explicit
column map. Each service's own status label is normalised to one of
``models.VERIFY_STATUSES`` via ``STATUS_WORDS`` and kept verbatim in
``raw_status``.
"""

from __future__ import annotations

import csv
import hashlib
import io
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from leadcore.models import (Contact, DomainPattern, EmailObservation, InferenceRun, Origin,
                             VERIFY_STATUSES, VerificationResult)
from leadcore.normalize.emails import canonicalise, parse

# A service's raw status word -> our normalised status. Lowercased, exact match
# first, then substring. Unknown words fall through to "unknown".
STATUS_WORDS = {
    "valid": "deliverable", "deliverable": "deliverable", "ok": "deliverable", "safe": "deliverable",
    "verified": "deliverable", "valid_email": "deliverable",
    "invalid": "undeliverable", "undeliverable": "undeliverable", "bad": "undeliverable",
    "does_not_exist": "undeliverable", "rejected": "undeliverable", "hardbounce": "undeliverable",
    "mailbox_not_found": "undeliverable", "not_found": "undeliverable", "no_such_user": "undeliverable",
    "catch-all": "catch_all", "catchall": "catch_all", "catch_all": "catch_all", "accept_all": "catch_all",
    "accept-all": "catch_all", "unknown": "unknown", "greylisted": "unknown", "risky": "unknown",
    "do_not_mail": "unknown", "spamtrap": "unknown", "abuse": "unknown", "no_mx": "no_mx",
    "no-mx": "no_mx", "nomx": "no_mx", "malformed": "invalid", "syntax_error": "invalid",
    "invalid_syntax": "invalid",
}


@dataclass(frozen=True)
class ColumnMap:
    """Which columns in a results file hold which field. Only ``email`` and ``status`` are required."""

    email: str = "email"
    status: str = "status"
    catch_all: str | None = None
    score: str | None = None


# Built-in layouts for common services. Add to these as new services are tried.
SERVICE_PRESETS: dict[str, ColumnMap] = {
    "zerobounce": ColumnMap(email="email", status="status", score="zb_score"),
    "neverbounce": ColumnMap(email="email", status="result"),
    "millionverifier": ColumnMap(email="email", status="result", score="quality"),
    "bouncer": ColumnMap(email="email", status="status"),
    "apollo": ColumnMap(email="email", status="email_status", catch_all="catchall"),
    "generic": ColumnMap(email="email", status="status", catch_all="catch_all", score="score"),
}

_TRUE = {"true", "1", "yes", "y", "catch-all", "catchall", "accept_all"}


def normalise_status(raw: str | None, *, catch_all: bool = False) -> str:
    if catch_all:
        return "catch_all"
    if not raw:
        return "unknown"
    key = raw.strip().lower().replace(" ", "_")
    if key in STATUS_WORDS:
        return STATUS_WORDS[key]
    for word, status in STATUS_WORDS.items():
        if word in key:
            return status
    return "unknown"


def result_key(email: str, service: str, checked_at: date) -> str:
    payload = f"{email.strip().lower()}\x1f{service}\x1f{checked_at.isoformat()}"
    return hashlib.sha256(payload.encode()).hexdigest()


@dataclass
class ImportStats:
    rows: int = 0
    imported: int = 0
    already_present: int = 0
    skipped_invalid: int = 0
    by_status: Counter = field(default_factory=Counter)


def _rows(content: str) -> list[dict]:
    return list(csv.DictReader(io.StringIO(content)))


def import_results(session: Session, content: str, *, service: str = "generic",
                   column_map: ColumnMap | None = None, checked_at: date | None = None,
                   source_file: str | None = None) -> ImportStats:
    """Import a verification results CSV (as text). Idempotent on (email, service, date)."""
    cmap = column_map or SERVICE_PRESETS.get(service, SERVICE_PRESETS["generic"])
    checked_at = checked_at or date.today()
    stats = ImportStats()
    existing = set(session.scalars(select(VerificationResult.result_key)))
    seen: set[str] = set()
    new: list[VerificationResult] = []
    for row in _rows(content):
        stats.rows += 1
        email = canonicalise(row.get(cmap.email, "")) if row.get(cmap.email) else None
        if not email:
            stats.skipped_invalid += 1
            continue
        catch = False
        if cmap.catch_all and str(row.get(cmap.catch_all, "")).strip().lower() in _TRUE:
            catch = True
        raw = row.get(cmap.status)
        status = normalise_status(raw, catch_all=catch)
        key = result_key(email, service, checked_at)
        if key in existing or key in seen:
            stats.already_present += 1
            continue
        seen.add(key)
        score = None
        if cmap.score and row.get(cmap.score):
            try:
                score = float(row[cmap.score])
            except ValueError:
                score = None
        extra = {k: v for k, v in row.items()
                 if k not in {cmap.email, cmap.status, cmap.catch_all, cmap.score} and v}
        new.append(VerificationResult(
            result_key=key, email=email, domain=parse(email).domain, status=status,
            raw_status=(raw or "").strip() or None, catch_all=catch or status == "catch_all",
            score=score, service=service, source_file=source_file, checked_at=checked_at,
            extra=extra or None))
        stats.by_status[status] += 1
    session.add_all(new)
    session.flush()
    stats.imported = len(new)
    return stats


def latest_status(session: Session, emails: list[str] | None = None) -> dict[str, VerificationResult]:
    """Most recent verification row per address (optionally restricted to ``emails``)."""
    q = select(VerificationResult).order_by(VerificationResult.email, VerificationResult.checked_at,
                                            VerificationResult.id)
    if emails:
        q = q.where(VerificationResult.email.in_([e.lower() for e in emails]))
    out: dict[str, VerificationResult] = {}
    for r in session.scalars(q):
        out[r.email] = r  # later rows overwrite, so the last one wins
    return out


EXPORT_HEADER = ("email", "domain", "first_name", "last_name", "variant_kind", "tier", "pattern")


def export_candidates(session: Session, run: InferenceRun, *, include_verified: bool = False
                      ) -> tuple[str, int]:
    """CSV of generated addresses needing a check: one row per candidate. Returns (csv, count).

    Skips addresses already verified (unless ``include_verified``) and addresses
    already observed (those are confirmed contacts).
    """
    verified = {e for e, r in latest_status(session).items()
                if r.status in ("deliverable", "undeliverable")} if not include_verified else set()
    observed = set(session.scalars(select(EmailObservation.email).where(EmailObservation.email.is_not(None))))
    patterns = {dp.id: dp for dp in session.scalars(
        select(DomainPattern).where(DomainPattern.run_id == run.id))}

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(EXPORT_HEADER)
    n = 0
    for c in session.scalars(select(Contact).where(Contact.origin == Origin.generated)
                             .order_by(Contact.domain, Contact.email)):
        if c.email in verified or c.email in observed:
            continue
        dp = patterns.get(c.derived_from_pattern_id)
        w.writerow([c.email, c.domain, c.first_name or "", c.last_name or "", c.variant_kind or "",
                    (dp.subtier or dp.tier) if dp else "", dp.pattern if dp else ""])
        n += 1
    return buf.getvalue(), n


def summary(session: Session) -> dict:
    """Counts by latest status, plus how many verification rows are stored."""
    total = session.scalar(select(func.count()).select_from(VerificationResult))
    latest = latest_status(session)
    by_status = Counter(r.status for r in latest.values())
    return {
        "rows_stored": total,
        "addresses": len(latest),
        "by_status": {s: by_status.get(s, 0) for s in VERIFY_STATUSES},
        "services": sorted({r.service for r in latest.values()}),
    }
