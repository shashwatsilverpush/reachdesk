"""Glue between the pure layers and the store: ingest, contact derivation, inference runs.

- ``ingest`` appends observations (idempotent on obs_key) and re-derives
  confirmed contacts. It never updates or deletes observations.
- ``run_inference`` writes a new immutable InferenceRun unless an identical
  run (same input fingerprint) already exists.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from leadcore import config
from leadcore.ingest import exclusions
from leadcore.ingest.base import Loader, RawRecord, obs_key
from leadcore.ingest.leadlist import LeadListLoader
from leadcore.ingest.reachouts import ReachoutsLoader
from leadcore.models import Contact, DomainPattern, EmailObservation, InferenceRun, Origin
from leadcore.normalize import emails
from leadcore.normalize.names import linkedin_surname, parse_display_name
from leadcore.patterns import inference
from leadcore.patterns.matcher import extract_surname

LOADERS = {"reachouts": ReachoutsLoader, "leadlist": LeadListLoader}
# Exclusion reasons that still describe a real business person.
CONTACT_REASONS = (None, "placeholder_name")


@dataclass
class IngestStats:
    loaded: int = 0
    inserted: int = 0
    already_present: int = 0
    contacts_created: int = 0
    contacts_updated: int = 0


def _to_observation(r: RawRecord) -> EmailObservation:
    email = emails.canonicalise(r.email_raw) if r.email_raw else None
    parsed = emails.parse(email) if email else None
    reason = exclusions.exclusion_reason(
        email, parsed and parsed.local, parsed and parsed.domain, r.first_name_raw
    )
    return EmailObservation(
        obs_key=obs_key(email or r.email_raw, r.source_file, r.row_index),
        email_raw=r.email_raw,
        email=email,
        local_part=parsed.local if parsed else None,
        domain=parsed.domain if parsed else None,
        first_name_raw=r.first_name_raw,
        stated_domain=emails.registrable_domain(r.company_raw),
        company_name_raw=r.company_raw,
        linkedin_url=r.linkedin_url,
        linkedin_surname=linkedin_surname(r.linkedin_url, r.first_name_raw),
        country=r.country,
        category=r.category,
        extra=r.extra or None,
        source=r.source,
        source_file=r.source_file,
        row_index=r.row_index,
        observed_at=r.observed_at,
        observed_at_precision=r.observed_at_precision,
        provenance_confirmed=r.provenance_confirmed,
        delivery_confirmed=False,
        excluded_reason=reason,
    )


def ingest(session: Session, sources: list[str] | None = None,
           loaders: list[Loader] | None = None) -> IngestStats:
    stats = IngestStats()
    loaders = loaders or [LOADERS[s]() for s in (sources or list(LOADERS))]
    existing = set(session.scalars(select(EmailObservation.obs_key)))
    new: list[EmailObservation] = []
    for loader in loaders:
        for rec in loader.load():
            stats.loaded += 1
            obs = _to_observation(rec)
            if obs.obs_key in existing:
                stats.already_present += 1
                continue
            existing.add(obs.obs_key)
            new.append(obs)
    session.add_all(new)
    session.flush()
    stats.inserted = len(new)
    c, u = sync_confirmed_contacts(session)
    stats.contacts_created, stats.contacts_updated = c, u
    return stats


def _best_name(obs: list[EmailObservation]) -> str | None:
    named = [o for o in obs if not exclusions.is_placeholder_name(o.first_name_raw)]
    if not named:
        return None
    # Prefer provenance-confirmed, then most recent, then earliest row.
    named.sort(key=lambda o: (not o.provenance_confirmed, -o.observed_at.toordinal(), o.source_file, o.row_index))
    return named[0].first_name_raw


def sync_confirmed_contacts(session: Session) -> tuple[int, int]:
    """Derive one confirmed contact per business address. Reads observations; never writes them."""
    by_email: dict[str, list[EmailObservation]] = defaultdict(list)
    for o in session.scalars(select(EmailObservation).where(EmailObservation.email.is_not(None))):
        if o.excluded_reason in CONTACT_REASONS:
            by_email[o.email].append(o)
    current = {c.email: c for c in session.scalars(select(Contact).where(Contact.origin == Origin.confirmed))}
    created = updated = 0
    for email, obs in sorted(by_email.items()):
        name = _best_name(obs)
        first = last = last_src = None
        if name:
            interp = parse_display_name(name)[0]
            first = interp.first
            if interp.last and interp.reading == "full_name":
                last, last_src = interp.last, "name_field"
        li = next((o.linkedin_surname for o in obs if o.linkedin_surname), None)
        if li:
            last, last_src = li, "linkedin"
        stated = next((o.stated_domain for o in obs if o.stated_domain), None)
        fields = dict(
            domain=obs[0].domain,
            first_name=first,
            provenance_confirmed=any(o.provenance_confirmed for o in obs),
            sources=sorted({o.source_file for o in obs}),
            stated_domain=stated,
        )
        c = current.get(email)
        if c is None:
            session.add(Contact(email=email, origin=Origin.confirmed, last_name=last,
                                last_name_source=last_src, **fields))
            created += 1
            continue
        changed = False
        for k, v in fields.items():
            if getattr(c, k) != v:
                setattr(c, k, v)
                changed = True
        # A residual surname written by inference is kept unless a better source appears.
        if last and (c.last_name, c.last_name_source) != (last, last_src):
            c.last_name, c.last_name_source = last, last_src
            changed = True
        updated += changed
    session.flush()
    return created, updated


# --- Inference ----------------------------------------------------------------------


def _views(session: Session) -> tuple[list[inference.ObservationView], set[str]]:
    views, domains = [], set()
    rows = session.execute(
        select(EmailObservation).where(EmailObservation.email.is_not(None))
        .order_by(EmailObservation.id)
    ).scalars()
    for o in rows:
        if o.excluded_reason in CONTACT_REASONS:
            domains.add(o.domain)
        if o.excluded_reason is not None:
            continue
        p = emails.parse(o.email)
        views.append(inference.ObservationView(
            email=o.email, domain=o.domain, match_local=p.match_local,
            first_name=o.first_name_raw, linkedin_surname=o.linkedin_surname,
            observed_at=o.observed_at, observed_at_precision=o.observed_at_precision,
            provenance_confirmed=o.provenance_confirmed, delivery_confirmed=o.delivery_confirmed,
            source=o.source,
        ))
    return views, domains


def tunables() -> dict:
    return {
        "as_of": config.as_of().isoformat(),
        "half_life": config.RECENCY_HALF_LIFE_YEARS,
        "source_weights": config.SOURCE_WEIGHTS,
        "source_provenance": config.SOURCE_PROVENANCE_CONFIRMED,
        "delivery_multiplier": config.DELIVERY_CONFIRMED_MULTIPLIER,
        "tier_a_min": config.TIER_A_MIN_SAMPLES,
        "tier_a_max_age": config.TIER_A_MAX_AGE_YEARS,
        "dominance": config.DOMINANCE_SHARE,
        "unexplained_is_dissent": config.UNEXPLAINED_IS_DISSENT,
        "engine_version": engine_version(),
    }


def engine_version() -> str:
    """Hash of every file that can change an inference result.

    Part of the run fingerprint, so editing the matcher, normaliser, exclusion
    lists or nickname table produces a new run instead of reusing a stale one.
    """
    from pathlib import Path

    pkg = Path(__file__).resolve().parent
    files = sorted([*pkg.glob("normalize/*.py"), *pkg.glob("patterns/*.py"),
                    pkg / "ingest" / "exclusions.py", pkg / "data" / "nicknames.csv"])
    h = hashlib.sha256()
    for f in files:
        h.update(f.name.encode())
        h.update(f.read_bytes())
    return h.hexdigest()[:16]


def _fingerprint(session: Session, use_nicknames: bool) -> str:
    keys = session.scalars(select(EmailObservation.obs_key).order_by(EmailObservation.obs_key)).all()
    h = hashlib.sha256()
    h.update(engine_version().encode())
    for k in keys:
        h.update(k.encode())
    h.update(json.dumps({"nick": use_nicknames, **tunables()}, sort_keys=True).encode())
    return h.hexdigest()


def current_run(session: Session, use_nicknames: bool | None = None) -> InferenceRun | None:
    q = select(InferenceRun).order_by(InferenceRun.id.desc())
    if use_nicknames is not None:
        q = q.where(InferenceRun.use_nicknames.is_(use_nicknames))
    return session.scalars(q.limit(1)).first()


def run_inference(session: Session, *, use_nicknames: bool = True, rebuild: bool = False
                  ) -> tuple[InferenceRun, bool, dict[str, inference.DomainResult]]:
    """Returns (run, created, results). Reuses an identical prior run unless rebuild."""
    fp = _fingerprint(session, use_nicknames)
    views, domains = _views(session)
    results = inference.infer(views, use_nicknames=use_nicknames, domains=domains)
    if not rebuild:
        existing = session.scalars(
            select(InferenceRun).where(InferenceRun.input_fingerprint == fp)
            .order_by(InferenceRun.id.desc()).limit(1)
        ).first()
        if existing:
            return existing, False, results

    held = dict(session.execute(
        select(Contact.domain, func.count()).where(Contact.origin == Origin.confirmed)
        .group_by(Contact.domain)
    ).all())
    run = InferenceRun(input_fingerprint=fp, use_nicknames=use_nicknames, config=tunables())
    session.add(run)
    session.flush()
    session.add_all(
        DomainPattern(
            run_id=run.id, domain=r.domain, tier=r.tier, pattern=r.pattern,
            sample_count=r.sample_count, weighted_support=r.weighted_support,
            dominance=r.dominance, patterns_seen=r.patterns_seen,
            unexplained_count=r.unexplained_count, newest_sample_at=r.newest_sample_at,
            migrated_from=r.migrated_from, evidence_email=r.evidence_email,
            contacts_held=held.get(r.domain, 0), note=r.note,
        )
        for r in results.values()
    )
    _write_residual_surnames(session, results, use_nicknames)
    session.flush()
    return run, True, results


def _write_residual_surnames(session: Session, results: dict[str, inference.DomainResult],
                             use_nicknames: bool) -> None:
    """Fill contacts.last_name from addresses at domains whose pattern carries a surname."""
    by_email = {}
    for r in results.values():
        if r.tier not in ("A", "B", "C") or not r.pattern:
            continue
        for s in r.samples:
            if r.pattern in s.patterns and s.first_name:
                sn = extract_surname(parse_display_name(s.first_name)[0].first, emails.parse(s.email).match_local,
                                     r.pattern, use_nicknames=use_nicknames)
                if sn:
                    by_email[s.email] = sn
    for c in session.scalars(select(Contact).where(Contact.origin == Origin.confirmed,
                                                  Contact.last_name_source.is_(None))):
        if c.email in by_email:
            c.last_name, c.last_name_source = by_email[c.email], "residual"


def tier_counts(session: Session, run: InferenceRun) -> Counter:
    return Counter(dict(session.execute(
        select(DomainPattern.tier, func.count()).where(DomainPattern.run_id == run.id)
        .group_by(DomainPattern.tier)
    ).all()))


def tier_map(session: Session, run: InferenceRun) -> dict[str, str]:
    return dict(session.execute(
        select(DomainPattern.domain, DomainPattern.tier).where(DomainPattern.run_id == run.id)
    ).all())
