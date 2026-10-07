"""Loader interface and the raw record every loader emits."""

from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date

import pandas as pd


@dataclass(frozen=True)
class RawRecord:
    """One email-bearing cell from one source row, before any judgement is applied."""

    source: str
    source_file: str          # file name, plus "#sheet" for multi-sheet workbooks
    row_index: int            # 1-based spreadsheet row number (header is row 1)
    email_raw: str | None
    first_name_raw: str | None
    company_raw: str | None = None
    linkedin_url: str | None = None
    country: str | None = None
    category: str | None = None
    observed_at: date = date(1970, 1, 1)
    observed_at_precision: str = "unknown"
    provenance_confirmed: bool = False
    extra: dict = field(default_factory=dict)


def obs_key(email: str | None, source_file: str, row_index: int) -> str:
    """Idempotency key: sha256 of (email, source_file, row_index)."""
    payload = f"{(email or '').strip().lower()}\x1f{source_file}\x1f{row_index}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def cell(v: object) -> str | None:
    """Normalise a pandas cell to a stripped string or None."""
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    s = str(v).strip()
    return s or None


class Loader(ABC):
    source: str

    @abstractmethod
    def load(self) -> list[RawRecord]:
        ...
