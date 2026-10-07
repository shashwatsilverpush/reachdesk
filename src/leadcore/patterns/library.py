"""The email local-part pattern library.

Each pattern is defined once, as a template of tokens, and both directions are
derived from that template: the generator (``pattern.render(first, last)``) and
the matcher's regex. That keeps generation and inference from drifting apart.

Template tokens:  F = full first name   f = first initial
                  L = full last name    l = last initial
                  . _ -  literal separators

Priors are rough real-world frequencies for B2B domains (they sum to ~1). They
only break ties between equally-supported patterns; they never outvote data.

``firstlastinitial`` from the original spec renders identically to ``firstl``
(jane + d -> janed), so it is registered as an alias rather than as a second
pattern: two patterns with identical output would make every such observation
permanently ambiguous.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class Pattern:
    name: str
    template: tuple[str, ...]
    prior: float

    @property
    def needs_last(self) -> bool:
        return any(t in ("L", "l") for t in self.template)

    @property
    def is_full_name(self) -> bool:
        """Uses both the whole first name and the whole surname (first.last, lastfirst...)."""
        return "F" in self.template and "L" in self.template

    @property
    def needs_first(self) -> bool:
        return any(t in ("F", "f") for t in self.template)

    def render(self, first: str, last: str | None) -> str | None:
        """The local part for (first, last), or None if a required part is missing."""
        if self.needs_last and not last:
            return None
        if self.needs_first and not first:
            return None
        parts = {"F": first, "f": first[:1], "L": last or "", "l": (last or "")[:1]}
        return "".join(parts.get(t, t) for t in self.template)

    def example(self, domain: str = "example.com") -> str:
        return f"{self.render('jane', 'doe')}@{domain}"

    @property
    def fn(self) -> Callable[[str, str | None], str | None]:
        return self.render


def _p(name: str, template: str, prior: float) -> Pattern:
    return Pattern(name, tuple(template), prior)


PATTERNS: tuple[Pattern, ...] = (
    _p("first.last", "F.L", 0.30),
    _p("first", "F", 0.20),
    _p("flast", "fL", 0.15),
    _p("firstlast", "FL", 0.08),
    _p("f.last", "f.L", 0.03),
    _p("firstl", "Fl", 0.03),
    _p("first_last", "F_L", 0.02),
    _p("first.l", "F.l", 0.02),
    _p("last.first", "L.F", 0.02),
    _p("first-last", "F-L", 0.01),
    _p("last", "L", 0.01),
    _p("lastfirst", "LF", 0.01),
    _p("lastf", "Lf", 0.01),
    _p("f_last", "f_L", 0.005),
    _p("last.f", "L.f", 0.005),
    _p("lfirst", "lF", 0.005),
    _p("first_l", "F_l", 0.003),
    _p("last_first", "L_F", 0.003),
    _p("f-last", "f-L", 0.002),
    _p("fl", "fl", 0.002),
    _p("l.first", "l.F", 0.002),
    _p("last_f", "L_f", 0.002),
    _p("f.l", "f.l", 0.001),
    _p("first-l", "F-l", 0.001),
    _p("last-first", "L-F", 0.001),
)

BY_NAME: dict[str, Pattern] = {p.name: p for p in PATTERNS}
ALIASES = {"firstlastinitial": "firstl"}


def get(name: str) -> Pattern:
    return BY_NAME[ALIASES.get(name, name)]


def render(name: str, first: str, last: str | None) -> str | None:
    return get(name).render(first, last)
