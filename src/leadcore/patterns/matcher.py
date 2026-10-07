"""Which patterns explain a given (name, local part) observation.

Neither source has a surname column, so the matcher supports **residual
inference**: decompose the local part against the known first name and treat
the remainder as the surname.

    Saurabh + saurabh.mahajan -> first.last   (surname "mahajan")
    Ted     + ttuttle         -> flast        (surname "tuttle")
    Rob     + rob             -> first        (no surname recoverable)

Several patterns are often *consistent* with one observation (``rob`` is also
``flast`` with surname "ob"). ``consistent()`` returns all of them;
``matches()`` returns the best explanation(s): those that account for the most
characters with known name material. A known surname that fits outranks any
residual reading. Ties are kept, so ``len(matches(...)) == 1`` is the
"resolves to exactly one pattern" test.

Pure string arithmetic. No I/O, no models.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache

from leadcore.normalize import nicknames as nick
from leadcore.normalize.names import given_variants, parse_display_name, surname_variants
from leadcore.patterns.library import PATTERNS, Pattern

# A residual surname: letters, optionally with internal hyphens/apostrophes.
_RESIDUAL_L = r"(?P<L>[a-z][a-z'\-]*[a-z])"  # >=2 chars; 1 char is an initial (l)
_RESIDUAL_l = r"(?P<l>[a-z])"
# Bonus that makes any known-surname match outrank every residual match.
_KNOWN_SURNAME_BONUS = 1000


@dataclass(frozen=True)
class Match:
    pattern: str
    first_variant: str
    surname: str | None          # residual or known surname as it appears in the local part
    surname_known: bool          # True if the surname came from outside the local part
    via_nickname: bool
    specificity: int


def _first_tokens(p: Pattern) -> int:
    return sum(1 for t in p.template if t in ("F", "f"))


@lru_cache(maxsize=4096)
def _residual_regex(pattern_name: str, first: str) -> re.Pattern | None:
    p = next(x for x in PATTERNS if x.name == pattern_name)
    if not p.needs_first:
        return None  # "last" alone cannot be inferred without a known surname
    seen_L = seen_l = False
    out = []
    for t in p.template:
        if t == "F":
            out.append(re.escape(first))
        elif t == "f":
            out.append(re.escape(first[0]))
        elif t == "L":
            out.append(_RESIDUAL_L if not seen_L else "(?P=L)")
            seen_L = True
        elif t == "l":
            out.append(_RESIDUAL_l if not seen_l else "(?P=l)")
            seen_l = True
        else:
            out.append(re.escape(t))
    return re.compile("^" + "".join(out) + "$")


def _specificity(p: Pattern, first: str, last: str | None, known: bool) -> int:
    score = 0
    for t in p.template:
        if t == "F":
            score += len(first)
        elif t == "f":
            score += 1
        elif known and t == "L":
            score += len(last or "")
        elif known and t == "l":
            score += 1
    return score + (_KNOWN_SURNAME_BONUS if known else 0)


def _first_forms(first: str, use_nicknames: bool) -> list[tuple[str, bool]]:
    forms: list[tuple[str, bool]] = []
    base = given_variants(first) or ([first] if first else [])
    for v in base:
        forms.append((v, False))
    if use_nicknames:
        for v in list(base):
            for alt in sorted(nick.expand(v)):
                if alt not in base and not any(alt == f for f, _ in forms):
                    forms.append((alt, True))
    return forms


def consistent(first: str, last: str | None, local: str, *, use_nicknames: bool = False) -> list[Match]:
    """Every (pattern, reading) consistent with the observation.

    ``first`` and ``last`` are raw name strings; variants are generated here.
    ``local`` should already be canonicalised (see normalize.emails.parse).
    """
    local = local.lower()
    out: list[Match] = []
    seen: set[tuple[str, str, str | None, bool]] = set()

    def add(m: Match) -> None:
        key = (m.pattern, m.first_variant, m.surname, m.surname_known)
        if key not in seen:
            seen.add(key)
            out.append(m)

    for fv, is_nick in _first_forms(first, use_nicknames):
        if not fv:
            continue
        if last:
            for lv in surname_variants(last):
                for p in PATTERNS:
                    # A one-letter surname is an initial: L-patterns would
                    # duplicate the l-patterns exactly.
                    if len(lv) == 1 and "L" in p.template:
                        continue
                    if p.render(fv, lv) == local:
                        add(Match(p.name, fv, lv, True, is_nick, _specificity(p, fv, lv, True)))
        for p in PATTERNS:
            rx = _residual_regex(p.name, fv)
            if rx is None:
                continue
            m = rx.match(local)
            if not m:
                continue
            gd = m.groupdict()
            surname = gd.get("L") or gd.get("l")
            if p.needs_last and not surname:
                continue
            add(Match(p.name, fv, surname, False, is_nick, _specificity(p, fv, surname, False)))
    return out


def _top(found: list[Match]) -> list[Match]:
    if not found:
        return []
    top = max(m.specificity for m in found)
    return [m for m in found if m.specificity == top]


# A plain reading that explains only the first initial ("nate" -> n + ate)
# is weak enough for a nickname reading to replace it.
_WEAK_SPECIFICITY = 1
# ...but only a nickname reading that accounts for at least this many characters.
_MIN_NICKNAME_SPECIFICITY = 3


def best(found: list[Match]) -> list[Match]:
    """The best explanation(s).

    Nickname readings exist to rescue mismatches, not to override good plain
    readings. So they are used only when the plain reading explains nothing,
    or explains just the initial and the nickname reading is substantial:

        Nathan + nate     -> first   (plain reading was flast with surname "ate")
        Robert + bob.smith -> first.last (no plain reading)
        Will   + williams -> firstlast  (plain reading stands; not firstl via William)
    """
    plain = _top([m for m in found if not m.via_nickname])
    nick = _top([m for m in found if m.via_nickname])
    if not plain:
        return nick
    if nick and plain[0].specificity <= _WEAK_SPECIFICITY:
        better = [m for m in nick if m.specificity >= _MIN_NICKNAME_SPECIFICITY]
        if better:
            return better
    return plain


def match_detail(name: str | None, local: str, *, last: str | None = None,
                 use_nicknames: bool = False) -> list[Match]:
    return list(_match_detail(name, local, last, use_nicknames))


@lru_cache(maxsize=200_000)
def _match_detail(name: str | None, local: str, last: str | None,
                  use_nicknames: bool) -> tuple[Match, ...]:
    """Best explanation(s) across every interpretation of a display name.

    Interpretations are ordered most-likely first (see parse_display_name). At
    equal specificity the earliest interpretation wins, so "Alex Mora" +
    alex.mora is first.last, not a surname-first last.first reading.
    """
    ranked: list[tuple[int, Match]] = []
    for i, interp in enumerate(parse_display_name(name, last)):
        for m in best(consistent(interp.first, interp.last, local, use_nicknames=use_nicknames)):
            ranked.append((i, m))
    if not ranked:
        return ()
    top = best([m for _, m in ranked])
    top_ids = {id(m) for m in top}
    first_rank = min(i for i, m in ranked if id(m) in top_ids)
    return tuple(m for i, m in ranked if id(m) in top_ids and i == first_rank)


def matches(first: str, last: str | None, local: str, *, use_nicknames: bool = False) -> list[str]:
    """Pattern names that best explain ``local`` for this person (deduped, stable order)."""
    names: list[str] = []
    for m in best(consistent(first, last, local, use_nicknames=use_nicknames)):
        if m.pattern not in names:
            names.append(m.pattern)
    return names


def extract_surname(first: str, local: str, pattern: str, *, use_nicknames: bool = False) -> str | None:
    """Read a surname out of an address already held, given the domain's pattern.

    Returns None when the pattern does not carry a full surname (``first``,
    ``firstl``...) or the local part does not fit the pattern for this name.
    """
    for m in consistent(first, None, local, use_nicknames=use_nicknames):
        if m.pattern == pattern and m.surname and len(m.surname) > 1:
            return m.surname
    return None
