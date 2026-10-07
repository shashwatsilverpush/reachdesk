"""Candidate address generation for named targets at domains with a usable pattern.

For a target (first, last, domain) whose domain is Tier A-C:
1. the primary candidate from the domain's dominant pattern;
2. nickname variants as separate candidates (capped, most common in our corpus first);
3. collision checks (see collisions.py) block targets whose addresses clash;
4. survivors are emitted as contacts with origin=generated, canary_status=pending.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from leadcore import config
from leadcore.generate.collisions import Collision, cross_collisions, self_collisions
from leadcore.models import Contact, DomainPattern, EmailObservation, Origin
from leadcore.normalize import nicknames
from leadcore.normalize.names import given_variants, surname_variants, tokens
from leadcore.patterns import library


@dataclass(frozen=True)
class Target:
    first: str
    last: str
    domain: str

    @property
    def label(self) -> str:
        return f"{self.first} {self.last}".strip()


@dataclass(frozen=True)
class Candidate:
    target: Target
    address: str
    kind: str            # primary | nickname
    first_variant: str


@dataclass
class GenerationResult:
    target: Target
    status: str                         # generated | blocked | not_generatable | already_held
    candidates: list[Candidate] = field(default_factory=list)
    collisions: list[Collision] = field(default_factory=list)
    reason: str | None = None


def candidates_for(target: Target, pattern: str, *, name_frequency: Counter | None = None,
                   nickname_limit: int | None = None) -> list[Candidate]:
    """Primary candidate plus nickname variants, deduplicated, primary first."""
    p = library.get(pattern)
    firsts = given_variants(target.first)
    lasts = surname_variants(target.last) if target.last else []
    if not firsts:
        return []
    first, last = firsts[0], (lasts[0] if lasts else None)
    out: list[Candidate] = []
    primary = p.render(first, last)
    if primary is None:
        return []
    out.append(Candidate(target, f"{primary}@{target.domain}", "primary", first))

    freq = name_frequency or Counter()
    limit = config.NICKNAME_GENERATION_LIMIT if nickname_limit is None else nickname_limit
    alts = sorted(nicknames.expand(first) - {first}, key=lambda n: (-freq.get(n, 0), n))
    for alt in alts[:limit]:
        local = p.render(alt, last)
        addr = f"{local}@{target.domain}"
        if local and all(c.address != addr for c in out):
            out.append(Candidate(target, addr, "nickname", alt))
    return out


def _name_frequency(session: Session) -> Counter:
    freq: Counter = Counter()
    for name in session.scalars(select(EmailObservation.first_name_raw).where(
            EmailObservation.excluded_reason.is_(None))):
        t = tokens(name or "")
        if t:
            freq[t[0]] += 1
    return freq


def generate(session: Session, targets: list[Target], run_id: int, *, persist: bool = True
             ) -> list[GenerationResult]:
    """Generate, collision-check and (optionally) persist candidates for targets."""
    freq = _name_frequency(session)
    domains = sorted({t.domain.lower() for t in targets})
    patterns = {dp.domain: dp for dp in session.scalars(
        select(DomainPattern).where(DomainPattern.run_id == run_id, DomainPattern.domain.in_(domains)))}
    observed: dict[str, list[EmailObservation]] = {}
    for o in session.scalars(select(EmailObservation).where(EmailObservation.domain.in_(domains))):
        observed.setdefault(o.email, []).append(o)

    results: dict[Target, GenerationResult] = {}
    pending: list[Candidate] = []
    for t in targets:
        t = Target(t.first, t.last, t.domain.lower())
        dp = patterns.get(t.domain)
        if dp is None or dp.tier not in config.GENERATABLE_TIERS or not dp.pattern:
            tier = dp.tier if dp else "none"
            results[t] = GenerationResult(t, "not_generatable", reason=f"domain tier {tier}")
            continue
        cands = candidates_for(t, dp.pattern, name_frequency=freq)
        if not cands:
            results[t] = GenerationResult(t, "not_generatable", reason=f"pattern {dp.pattern} needs a surname")
            continue
        results[t] = GenerationResult(t, "generated", candidates=cands)
        pending.extend(cands)

    blocked: dict[Target, list[Collision]] = {}
    for col in cross_collisions(pending, observed) + self_collisions(pending):
        for tgt in col.targets:
            blocked.setdefault(tgt, []).append(col)

    for t, res in results.items():
        if res.status != "generated":
            continue
        if t in blocked:
            res.status, res.collisions, res.reason = "blocked", blocked[t], blocked[t][0].kind + "-collision"
            res.candidates = []
            continue
        # Cross-collision has already ruled out a different owner, so any hit
        # here is this person's (or an unattributed) address: we hold them already.
        held = [c.address for c in res.candidates if c.address in observed]
        if held:
            res.status, res.reason, res.candidates = "already_held", f"observed as {held[0]}", []

    if persist:
        _persist(session, results.values(), patterns, blocked)
    return list(results.values())


def _persist(session: Session, results, patterns: dict[str, DomainPattern], blocked) -> None:
    from leadcore.models import GenerationFlag

    existing = set(session.scalars(select(Contact.email)))
    for res in results:
        for c in res.candidates:
            if c.address in existing:
                continue
            existing.add(c.address)
            session.add(Contact(
                email=c.address, domain=res.target.domain, first_name=c.first_variant,
                last_name=res.target.last or None, last_name_source="target",
                origin=Origin.generated, provenance_confirmed=True,
                derived_from_pattern_id=patterns[res.target.domain].id, variant_kind=c.kind,
            ))
        for col in res.collisions:
            other = [t.label for t in col.targets if t != res.target]
            session.add(GenerationFlag(
                domain=res.target.domain, address=col.address, kind=col.kind,
                target_name=res.target.label,
                other_name=col.observed_name or (other[0] if other else None),
                observation_id=col.observation_id,
            ))
    session.flush()
