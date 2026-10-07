"""Display-name normalisation.

This module decides engine accuracy more than inference does: a pattern can
only be recognised if the name tokens are rendered the way a mail admin would
have rendered them. All output is lowercase ASCII.

Main entry points:
- ``fold(s)``             unicode -> ascii token text (accents, non-Latin scripts)
- ``surname_variants(s)``  every plausible local-part rendering of a surname
- ``given_variants(s)``    every plausible rendering of a given name
- ``parse_display_name``   a display name -> ordered list of interpretations
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

from anyascii import anyascii

# Lowercase surname particles. Joined, dropped and initialised forms are all
# produced, since mail systems disagree on how to treat them.
PARTICLES = frozenset(
    {
        "van", "von", "der", "den", "de", "del", "della", "di", "da", "dos", "das",
        "du", "la", "le", "ter", "ten", "bin", "binti", "al", "el", "st", "mac",
    }
)
# Dutch "der"/"den" are routinely interchanged in practice.
_PARTICLE_SWAPS = {"der": "den", "den": "der"}

_NON_TOKEN = re.compile(r"[^a-z0-9\s'\-.]")
_SPACE = re.compile(r"\s+")


def strip_marks(s: str) -> str:
    """NFKD-decompose and drop combining marks (é -> e). Non-Latin left intact."""
    decomposed = unicodedata.normalize("NFKD", s)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def fold(s: str) -> str:
    """Unicode text -> lowercase ASCII, keeping spaces, hyphens, dots, apostrophes."""
    if not s:
        return ""
    s = strip_marks(s)
    # Special-case letters NFKD does not decompose.
    s = s.translate(str.maketrans({"ß": "ss", "ø": "o", "Ø": "O", "æ": "ae", "Æ": "AE",
                                   "ł": "l", "Ł": "L", "đ": "d", "Đ": "D", "ı": "i"}))
    s = anyascii(s).lower()
    s = s.replace("’", "'").replace("`", "'")
    s = _NON_TOKEN.sub(" ", s)
    return _SPACE.sub(" ", s).strip()


def _alnum(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s)


def tokens(s: str) -> list[str]:
    """Split a folded name into word tokens. Dots split initials (``r.k.`` -> r, k)."""
    out = []
    for raw in fold(s).replace(".", " ").split():
        t = raw.strip("'-")
        if t:
            out.append(t)
    return out


def surname_variants(surname: str) -> list[str]:
    """Plausible local-part renderings of a surname, most canonical first.

    ``Smith-Jones``   -> smithjones, smith-jones, smith, jones
    ``van der Berg``  -> vanderberg, vandenberg, berg, vberg, (+ van-der-berg, vdberg)
    ``O'Brien``       -> obrien, brien
    """
    toks = tokens(surname)
    if not toks:
        return []
    out: list[str] = []

    def add(v: str) -> None:
        if v and v not in out:
            out.append(v)

    # Hyphen/apostrophe-bearing tokens: joined, hyphenated, and each part.
    parts: list[str] = []
    for t in toks:
        sub = [p for p in re.split(r"['\-]", t) if p]
        parts.extend(sub)

    add("".join(_alnum(p) for p in parts))
    if any("-" in t for t in toks):
        add("-".join(x for x in (_alnum(y) for y in re.split(r"[\s\-]+", " ".join(toks))) if x))

    particles = [p for p in parts if p in PARTICLES]
    roots = [p for p in parts if p not in PARTICLES]

    if particles and roots:
        swapped = [_PARTICLE_SWAPS.get(p, p) if p in PARTICLES else p for p in parts]
        add("".join(swapped))
        add("".join(roots))
        add(particles[0][0] + "".join(roots))
        add("".join(p[0] for p in particles) + "".join(roots))
        add("-".join(parts))
    if len(roots) > 1 or (len(parts) > 1 and not particles):
        for p in roots or parts:
            add(p)
    # O'Brien / D'Souza: the apostrophe-joined token minus its leading letter.
    for t in toks:
        if "'" in t:
            pre, _, post = t.partition("'")
            if len(pre) == 1 and post:
                add(_alnum(post))
    return [v for v in out if len(v) >= 1]


def given_variants(given: str) -> list[str]:
    """Plausible renderings of a given name. ``Jean-Pierre`` -> jeanpierre, jean-pierre, jean."""
    toks = tokens(given)
    if not toks:
        return []
    out: list[str] = []

    def add(v: str) -> None:
        if v and v not in out:
            out.append(v)

    first = toks[0]
    pieces = [p for p in re.split(r"['\-]", first) if p]
    add("".join(pieces))
    if "-" in first:
        add("-".join(pieces))
        add(pieces[0])
        add("".join(p[0] for p in pieces))  # Jean-Pierre -> jp
    if len(toks) > 1:  # "Mary Ann" as a compound given name
        add("".join(_alnum(t) for t in toks))
    return out


@dataclass(frozen=True)
class NameInterpretation:
    """One reading of a display name. ``last`` is None when no surname is known."""

    first: str
    last: str | None = None
    middle: tuple[str, ...] = ()
    reading: str = "as_given"  # as_given | full_name | surname_first | initials_expanded
    first_variants: tuple[str, ...] = field(default=())
    last_variants: tuple[str, ...] = field(default=())


def _interp(first: str, last: str | None, middle=(), reading="as_given") -> NameInterpretation:
    return NameInterpretation(
        first=first,
        last=last,
        middle=tuple(middle),
        reading=reading,
        first_variants=tuple(given_variants(first)),
        last_variants=tuple(surname_variants(last)) if last else (),
    )


def parse_display_name(name: str | None, last: str | None = None) -> list[NameInterpretation]:
    """Turn a raw name field (and optional separate surname) into interpretations.

    Ordered most-likely first. Handles:
    - single-name records:           ``Saurabh``          -> first only
    - full names in a first-name col: ``John Smith``       -> john / smith
    - middle names and initials:      ``John Q. Smith``    -> john / smith, middle q
    - Indian expanded initials:       ``R.K. Sharma``      -> sharma (initials r,k) and
                                      ``K. Ramesh``        -> ramesh / k
    - surname-first ordering:         ``Sharma Rahul``     -> also rahul / sharma
    - particles:                      ``Jan van der Berg`` -> jan / van der berg
    """
    if last:
        toks = tokens(name or "")
        if not toks:
            return []
        out = [_interp(toks[0], last, toks[1:])]
        if len(toks) > 1:
            out.append(_interp(" ".join(toks), last, reading="compound_given"))
        return out

    toks = tokens(name or "")
    if not toks:
        return []
    if len(toks) == 1:
        return [_interp(toks[0], None)]

    out: list[NameInterpretation] = []
    initials = [t for t in toks if len(t) == 1]
    words = [t for t in toks if len(t) > 1]

    if initials and len(words) == 1:
        # "R.K. Sharma" / "K Ramesh" / "Ramesh K": one real word plus initials.
        # South-Indian usage puts the given name in the word and the family/
        # place initial(s) in front; mail systems render it ramesh.k or kramesh.
        word = words[0]
        out.append(_interp(word, "".join(initials), reading="initials_expanded"))
        out.append(_interp(initials[0], word, middle=initials[1:], reading="initials_expanded"))
        out.append(_interp(word, None, reading="initials_expanded"))
        return out

    # Find where a particle-led surname starts: "jan van der berg".
    split = len(toks) - 1
    for i in range(1, len(toks)):
        if toks[i] in PARTICLES and i < len(toks) - 1:
            split = i
            break
    first, last_toks, middle = toks[0], toks[split:], toks[1:split]
    out.append(_interp(first, " ".join(last_toks), middle, reading="full_name"))
    # Surname-first ordering ("Sharma Rahul").
    if len(toks) == 2:
        out.append(_interp(toks[1], toks[0], reading="surname_first"))
    # A compound given name with no surname ("Mary Ann").
    out.append(_interp(" ".join(toks), None, reading="compound_given"))
    return out


def linkedin_surname(url: str | None, first: str | None) -> str | None:
    """Surname from a LinkedIn profile slug, only when the slug confirms the first name.

    ``/in/alex-mora-92742a11b/``        + Alex     -> mora
    ``/in/deya-narv%C3%A1ez-2b649739/`` + Deya     -> narvaez
    ``/in/harrisonweinhold/``           + Harrison -> weinhold
    ``/in/valenciadepaz/``              + Rommel   -> None (slug does not start with the name)
    """
    from urllib.parse import unquote

    from leadcore.normalize.nicknames import expand

    if not url or not first:
        return None
    m = re.search(r"linkedin\.com/(?:in|pub)/([^/?#]+)", str(url), re.I)
    if not m:
        return None
    pieces = (_alnum(fold(p)) for p in re.split(r"[-_]", unquote(m.group(1))))
    # Drop empty pieces and any carrying digits (the trailing profile id).
    parts = [p for p in pieces if p and not re.search(r"\d", p)]
    if not parts:
        return None
    firsts = set()
    for v in given_variants(first):
        firsts |= expand(v)
    if len(parts) >= 2:
        if parts[0] in firsts:
            rest = [p for p in parts[1:] if len(p) > 1] or parts[1:]
            return " ".join(rest) if rest and len(rest[-1]) > 1 else None
        if parts[-1] in firsts and len(parts[0]) > 1:
            return parts[0]  # surname-first slug
        return None
    single = parts[0]
    for f in sorted(firsts, key=len, reverse=True):
        if single.startswith(f) and len(single) - len(f) >= 2:
            return single[len(f):]
    return None
