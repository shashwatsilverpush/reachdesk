"""Bidirectional given-name equivalence, from a vendored public dataset.

Both directions matter:
- inference: a confirmed ``bob.smith@`` for someone recorded as Robert must
  register as ``first.last`` rather than as a mismatch;
- generation: a target named Robert yields ``robert.smith@`` *and*
  ``bob.smith@`` as separate candidates.

Expansion is one hop in each direction (canonical -> nicknames, nickname ->
canonicals). It is deliberately not transitive: the source lists
``robert -> bill`` and ``william -> bill``, and a closure would make Robert
equivalent to William.
"""

from __future__ import annotations

import csv
from collections import defaultdict
from functools import lru_cache

from leadcore.config import VENDORED


@lru_cache(maxsize=1)
def _tables() -> tuple[dict[str, frozenset[str]], dict[str, frozenset[str]]]:
    nick_of: dict[str, set[str]] = defaultdict(set)
    canon_of: dict[str, set[str]] = defaultdict(set)
    with open(VENDORED / "nicknames.csv", newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            if row["relationship"] != "has_nickname":
                continue
            canon, nick = row["name1"].strip().lower(), row["name2"].strip().lower()
            if canon and nick and canon != nick:
                nick_of[canon].add(nick)
                canon_of[nick].add(canon)
    return (
        {k: frozenset(v) for k, v in nick_of.items()},
        {k: frozenset(v) for k, v in canon_of.items()},
    )


def nicknames_of(name: str) -> frozenset[str]:
    return _tables()[0].get(name.lower(), frozenset())


def canonicals_of(name: str) -> frozenset[str]:
    return _tables()[1].get(name.lower(), frozenset())


def expand(name: str) -> set[str]:
    """The name plus all known one-hop equivalents, lowercased."""
    n = name.strip().lower()
    if not n:
        return set()
    return {n} | set(nicknames_of(n)) | set(canonicals_of(n))


def are_equivalent(a: str, b: str) -> bool:
    return b.strip().lower() in expand(a)
