"""Paths, database URL and every tunable the engine uses.

Everything numeric that affects tier assignment lives here so a run can be
reproduced from (observations, config) alone. Values are module-level
constants; override the DB URL with LEADCORE_DB_URL.
"""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATA_RAW = ROOT / "data" / "raw"
DATA_OUT = ROOT / "data" / "out"
VENDORED = Path(__file__).resolve().parent / "data"

REACHOUTS_FILE = DATA_RAW / "Reachouts_.xlsx"
LEADLIST_FILE = DATA_RAW / "Lead_list.xlsx"
# Verified addresses revealed through Apollo (optional; skipped if absent).
APOLLO_FILE = DATA_RAW / "apollo_verified.csv"

DB_URL = os.environ.get("LEADCORE_DB_URL", f"sqlite:///{ROOT / 'leadcore.db'}")

# --- Observation dating -------------------------------------------------------
# Neither source has row-level dates. Reachouts sheets are named by year, so a
# row is dated to 1 January of its sheet year (precision "year"). Lead_list has
# no date at all; it is dated to this fixed value (precision "assumed") rather
# than the file mtime, which changes whenever the file is re-saved and would
# make re-ingestion non-deterministic.
REACHOUTS_SHEET_DATES = {"2025": date(2025, 1, 1), "2026": date(2026, 1, 1)}
LEADLIST_ASSUMED_DATE = date(2025, 1, 1)

# "Now" for recency maths. Pinned so tiering is reproducible; set to None to
# use today's date.
AS_OF: date | None = date(2026, 10, 7)

# --- Provenance ---------------------------------------------------------------
# Both files come from our own systems: Reachouts_ is the sequenced outreach
# log, Lead_list is an export of our own (less curated) contact database.
SOURCE_PROVENANCE_CONFIRMED = {"reachouts": True, "leadlist": True, "apollo": True}
# Confidence class a provenance-confirmed source contributes to sample weight.
SOURCE_CLASS = {"reachouts": "own_correspondence", "leadlist": "own_database", "apollo": "verified_provider"}

# --- Sample weighting -----------------------------------------------------------
RECENCY_HALF_LIFE_YEARS = 3.0
SOURCE_WEIGHTS = {
    "linkedin_confirmed": 1.0,   # surname independently confirmed (LinkedIn slug or provider record)
    "own_correspondence": 0.8,   # sequenced outreach log (Reachouts_)
    "verified_provider": 0.8,    # address verified by a data provider (Apollo)
    "own_database": 0.65,        # own contact database, less curated (Lead_list)
    "unknown_provenance": 0.5,   # origin not established
}
DELIVERY_CONFIRMED_MULTIPLIER = 1.5

# --- Tiering --------------------------------------------------------------------
TIER_A_MIN_SAMPLES = 3
TIER_A_MAX_AGE_YEARS = 2.0
# A pattern is "dominant" when it carries at least this share of the domain's
# total sample weight. Below it, the domain is a conflict (Tier D).
DOMINANCE_SHARE = 0.75
# An address with a usable first name that no pattern explains counts as
# dissent: it adds to the sample count and to the total weight without
# supporting any pattern. Conservative: a domain is not called consistent
# while it holds addresses we cannot account for. False = ignore them.
UNEXPLAINED_IS_DISSENT = True

# --- Generation -----------------------------------------------------------------
GENERATABLE_TIERS = ("A", "B", "C")
# Tier C is split by the shape of its single known address. Validation
# (2026-10-07): 15/16 right when it is a full-name format (C+), 7/16 when it is
# first@ or initial-based (C-), usually because that one address is a founder's.
# For C- firms generation adds this fallback format as an extra candidate so
# the test send can decide between them.
C_MINUS_FALLBACK_PATTERN = "first.last"
# Cap on nickname variants emitted per target (ordered by corpus frequency).
NICKNAME_GENERATION_LIMIT = 4

# --- Reports --------------------------------------------------------------------
CAPTURE_SHEET_PER_TIER = 24
CAPTURE_SHEET_SEED = 7


def as_of() -> date:
    return AS_OF or date.today()
