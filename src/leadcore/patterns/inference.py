"""Per-domain pattern aggregation, weighting and tiering.

Pure functions over plain records; persistence lives in ``leadcore.pipeline``.

A *sample* is one distinct address (observations of the same address across
rows/files are merged; the address keeps its strongest weight and newest
date). A sample is *usable* if the matcher explains it. Unexplained samples (e.g.
"nate" for a Nathan before the nickname pass) support no pattern; when
``config.UNEXPLAINED_IS_DISSENT`` is set they still count toward the sample
total and the weight denominator, so they dilute dominance.

Sample weight = recency x source confidence x delivery confirmation:
- recency: 0.5 ** (age_years / half_life)
- source:  linkedin_confirmed > own_correspondence > own_database > unknown_provenance
- delivery: x DELIVERY_CONFIRMED_MULTIPLIER when delivery was confirmed

Tiering (sample counts are distinct usable addresses; weights decide dominance):
  A  >= 3 samples, one pattern carries >= DOMINANCE_SHARE of weight, newest < 2y
  B  2 samples that agree; or the A case with a stale newest sample;
     or a D whose conflict is a clean temporal split (convention migration)
  C  1 sample; sub-tier C+ if that address is a full-name format, else C-
  D  samples support conflicting patterns
  E  no usable samples
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from datetime import date
from typing import Iterable

from leadcore import config
from leadcore.ingest.exclusions import is_placeholder_name
from leadcore.patterns import library
from leadcore.normalize.names import surname_variants
from leadcore.patterns.matcher import Match, best, match_detail


def _compatible_surname(residual: str | None, known: str) -> bool:
    """Could ``residual`` (read off an address) be a rendering of the known surname?

    Accepts transliterations (khalifh ~ khalifeh), truncations (st <- stein)
    and parts (dias <- almeidadias); rejects an invented different surname
    (hu for Doan, enni for Minella).
    """
    if not residual:
        return True  # the reading does not use a surname at all ("first")
    for v in surname_variants(known):
        if len(residual) == 1:
            if v.startswith(residual):
                return True
        elif residual in v or SequenceMatcher(None, residual, v).ratio() >= 0.8:
            return True
    return False


@dataclass(frozen=True)
class ObservationView:
    """The subset of an observation that inference needs."""

    email: str
    domain: str
    match_local: str
    first_name: str | None
    known_surname: str | None       # from a LinkedIn slug or a provider record
    observed_at: date
    observed_at_precision: str
    provenance_confirmed: bool
    delivery_confirmed: bool = False
    source: str | None = None
    # True when known_surname is a provider record (reliable), False when it
    # was parsed from a LinkedIn slug (often junk: "jamesy", "robinsonsnapshot").
    surname_from_provider: bool = False


@dataclass
class Sample:
    email: str
    patterns: tuple[str, ...]              # best explanation(s); empty = unexplained
    weight: float
    newest: date
    dated: bool                            # newest date is real, not assumed
    source_class: str
    surname: str | None = None
    surname_source: str | None = None      # linkedin | name_field | residual
    via_nickname: bool = False
    first_name: str | None = None


@dataclass
class DomainResult:
    domain: str
    tier: str
    pattern: str | None
    sample_count: int
    weighted_support: float
    dominance: float | None
    patterns_seen: dict[str, int]
    unexplained_count: int
    newest_sample_at: date | None
    migrated_from: str | None = None
    subtier: str | None = None             # C+ / C- for Tier C, else None
    evidence_email: str | None = None
    note: str | None = None
    samples: list[Sample] = field(default_factory=list, repr=False)


def _years(a: date, b: date) -> float:
    return (b - a).days / 365.25


def recency_weight(observed: date, as_of: date, half_life: float | None = None) -> float:
    hl = half_life or config.RECENCY_HALF_LIFE_YEARS
    return 0.5 ** (max(0.0, _years(observed, as_of)) / hl)


def _source_class(obs: ObservationView, m: Match | None, linkedin_used: bool) -> str:
    if m is not None and m.surname_known and linkedin_used:
        return "linkedin_confirmed"
    if not obs.provenance_confirmed:
        return "unknown_provenance"
    return config.SOURCE_CLASS.get(obs.source or "", "own_correspondence")


def build_samples(observations: Iterable[ObservationView], *, use_nicknames: bool,
                  as_of: date | None = None) -> dict[str, list[Sample]]:
    """Group usable observations into per-domain samples (one per distinct address)."""
    as_of = as_of or config.as_of()
    per_email: dict[str, list[tuple[ObservationView, list[Match], bool]]] = defaultdict(list)
    domain_of: dict[str, str] = {}
    for o in observations:
        if is_placeholder_name(o.first_name):
            continue
        linkedin_used = bool(o.known_surname)
        found = match_detail(o.first_name, o.match_local, last=o.known_surname,
                             use_nicknames=use_nicknames)
        if linkedin_used and not any(m.surname_known for m in found):
            # The known surname did not fit exactly: fall back to reading the
            # address on its own. A provider surname is trusted, so a reading
            # that invents a different surname is rejected; a slug surname is
            # not, so the address's own reading wins.
            found = match_detail(o.first_name, o.match_local, use_nicknames=use_nicknames)
            if o.surname_from_provider:
                found = [m for m in found if _compatible_surname(m.surname, o.known_surname)]
            linkedin_used = False
        per_email[o.email].append((o, found, linkedin_used))
        domain_of[o.email] = o.domain

    out: dict[str, list[Sample]] = defaultdict(list)
    for email, rows in per_email.items():
        # Best explanation across every row that mentions this address.
        all_matches = [m for _, found, _ in rows for m in found]
        top = best(all_matches)
        patterns = tuple(sorted({m.pattern for m in top}, key=lambda p: -library.get(p).prior))
        weight, src = 0.0, "unknown_provenance"
        for o, found, li in rows:
            m = next((x for x in found if x in top), found[0] if found else None)
            sc = _source_class(o, m, li)
            w = recency_weight(o.observed_at, as_of) * config.SOURCE_WEIGHTS[sc]
            if o.delivery_confirmed:
                w *= config.DELIVERY_CONFIRMED_MULTIPLIER
            if w > weight:
                weight, src = w, sc
        newest_row = max(rows, key=lambda r: r[0].observed_at)[0]
        lead = top[0] if top else None
        surname_source = None
        if lead and lead.surname and len(lead.surname) > 1:
            if lead.surname_known:
                surname_source = "linkedin" if any(li for _, _, li in rows) else "name_field"
            else:
                surname_source = "residual"
        out[domain_of[email]].append(
            Sample(
                email=email,
                patterns=patterns,
                weight=weight,
                newest=newest_row.observed_at,
                dated=any(r[0].observed_at_precision != "assumed" and
                          r[0].observed_at == newest_row.observed_at for r in rows),
                source_class=src,
                surname=lead.surname if surname_source else None,
                surname_source=surname_source,
                via_nickname=bool(lead and lead.via_nickname),
                first_name=rows[0][0].first_name,
            )
        )
    return out


def _support(samples: list[Sample]) -> dict[str, float]:
    sup: dict[str, float] = defaultdict(float)
    for s in samples:
        for p in s.patterns:
            sup[p] += s.weight
    return sup


def _leader(sup: dict[str, float]) -> str:
    return max(sup, key=lambda p: (round(sup[p], 9), library.get(p).prior, p))


def temporal_split(samples: list[Sample]) -> tuple[str, str] | None:
    """If conflicting patterns segregate cleanly by date, return (old, new).

    Clean means: there is a cutoff date such that every sample strictly after it
    supports one pattern P_new, and no sample on or before it supports P_new.
    Samples whose date is assumed rather than observed must support P_new, since
    they cannot be placed on either side of the cutoff with evidence. Every
    sample before the cutoff must be explained: an unexplained address is not
    evidence of an older convention.
    """
    dated = [s for s in samples if s.dated]
    undated = [s for s in samples if not s.dated]
    dates = sorted({s.newest for s in dated})
    for cut in dates[:-1]:
        after = [s for s in dated if s.newest > cut]
        before = [s for s in dated if s.newest <= cut]
        if not all(s.patterns for s in before):
            continue
        common = set.intersection(*(set(s.patterns) for s in after))
        for new in sorted(common, key=lambda p: -library.get(p).prior):
            if any(new in s.patterns for s in before):
                continue
            if any(new not in s.patterns for s in undated):
                continue
            old = _leader(_support(before))
            return old, new
    return None


def tier_domain(domain: str, samples: list[Sample], *, as_of: date | None = None) -> DomainResult:
    as_of = as_of or config.as_of()
    usable = [s for s in samples if s.patterns]
    unexplained = len(samples) - len(usable)
    seen: dict[str, int] = defaultdict(int)
    for s in usable:
        for p in s.patterns:
            seen[p] += 1
    seen = dict(sorted(seen.items(), key=lambda kv: (-kv[1], kv[0])))
    newest = max((s.newest for s in usable), default=None)

    def result(tier: str, pattern: str | None, dominance: float | None, **kw) -> DomainResult:
        ev = None
        if pattern:
            sup = [s for s in usable if pattern in s.patterns]
            if sup:
                ev = max(sup, key=lambda s: (s.weight, s.newest, s.email)).email
        n = len(samples) if config.UNEXPLAINED_IS_DISSENT else len(usable)
        return DomainResult(domain, tier, pattern, n, round(sum(s.weight for s in usable), 6),
                            None if dominance is None else round(dominance, 6), seen, unexplained,
                            newest, evidence_email=ev, samples=samples, **kw)

    if not usable:
        return result("E", None, None)

    counted = samples if config.UNEXPLAINED_IS_DISSENT else usable
    sup = _support(usable)
    total = sum(s.weight for s in counted)
    lead = _leader(sup)
    share = sup[lead] / total if total else 0.0

    if len(counted) == 1:
        note = "ambiguous single sample" if len(usable[0].patterns) > 1 else None
        sub = "C+" if library.get(lead).is_full_name else "C-"
        return result("C", lead, share, note=note, subtier=sub)
    if len(counted) == 2:
        if share >= 1.0 - 1e-9:
            return result("B", lead, share)
    elif len(counted) >= config.TIER_A_MIN_SAMPLES and share >= config.DOMINANCE_SHARE:
        if newest and _years(newest, as_of) < config.TIER_A_MAX_AGE_YEARS:
            return result("A", lead, share)
        return result("B", lead, share, note="dominant but stale")

    split = temporal_split(counted)
    if split:
        old, new = split
        return result("B", new, sup[new] / total, migrated_from=old, note="convention migration")
    return result("D", lead, share)


def infer(observations: Iterable[ObservationView], *, use_nicknames: bool,
          as_of: date | None = None, domains: Iterable[str] = ()) -> dict[str, DomainResult]:
    """Tier every domain. ``domains`` adds domains with no usable observations (-> Tier E)."""
    as_of = as_of or config.as_of()
    by_domain = build_samples(observations, use_nicknames=use_nicknames, as_of=as_of)
    out = {d: tier_domain(d, s, as_of=as_of) for d, s in sorted(by_domain.items())}
    for d in sorted(set(domains) - set(out)):
        out[d] = tier_domain(d, [], as_of=as_of)
    return out
