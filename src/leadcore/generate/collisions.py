"""Collision detection for generated candidates.

- Cross-collision: a candidate address is already observed for a *different*
  person. The domain must use a disambiguation convention (john.smith2,
  middle initials...) that we have not inferred, so the target is blocked and
  the observed person is flagged alongside it.
- Self-collision: two targets at the same domain produce the same address.
  Both are blocked.

"Different person" is decided on first names (nickname-aware): an observed
address whose recorded first name is equivalent to the target's is the same
person and is not a collision. An address observed only under a placeholder
name has no attribution and is never a cross-collision.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import TYPE_CHECKING

from leadcore.ingest.exclusions import is_placeholder_name
from leadcore.normalize import nicknames
from leadcore.normalize.names import given_variants

if TYPE_CHECKING:
    from leadcore.generate.candidates import Candidate, Target


@dataclass(frozen=True)
class Collision:
    kind: str                       # cross | self
    address: str
    targets: tuple["Target", ...]
    observed_name: str | None = None
    observation_id: int | None = None


def _given(name: str | None) -> set[str]:
    out: set[str] = set()
    for v in given_variants(name or ""):
        out |= nicknames.expand(v)
    return out


def same_person(name_a: str | None, name_b: str | None) -> bool:
    a, b = _given(name_a), _given(name_b)
    return bool(a and b and a & b)


def cross_collisions(candidates: list["Candidate"], observed: dict[str, list]) -> list[Collision]:
    """``observed`` maps address -> observations (anything with .first_name_raw and .id)."""
    out = []
    for c in candidates:
        for o in observed.get(c.address, []):
            if is_placeholder_name(o.first_name_raw):
                continue
            if not same_person(c.target.first, o.first_name_raw):
                out.append(Collision("cross", c.address, (c.target,), o.first_name_raw, o.id))
                break
    return out


def self_collisions(candidates: list["Candidate"]) -> list[Collision]:
    by_addr: dict[str, list["Candidate"]] = defaultdict(list)
    for c in candidates:
        by_addr[c.address].append(c)
    out = []
    for addr, cs in sorted(by_addr.items()):
        targets = tuple(dict.fromkeys(c.target for c in cs))
        if len(targets) > 1:
            out.append(Collision("self", addr, targets))
    return out
