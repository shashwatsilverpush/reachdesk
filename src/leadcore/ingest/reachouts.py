"""Loader for Reachouts_.xlsx (sheets "2025" and "2026").

Quirks handled:
- ``Company Name`` is group-filled (first row of each company block only) and
  is forward-filled within each sheet.
- ``Company Name`` holds a domain string that disagrees with the email domain
  ~20% of the time. It is kept verbatim as ``company_raw`` (and later as
  ``stated_domain``); everything downstream keys on the *email* domain.
- Unnamed trailing columns hold stray notes (Play Store URLs, sequencer lead
  IDs). They are preserved in ``extra`` rather than dropped.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from leadcore import config
from leadcore.ingest.base import Loader, RawRecord, cell
from leadcore.normalize.emails import split_cell

EXPECTED = ["Company Name", "First Name", "Mail Address", "Linkedin Address", "Country", "Category"]


class ReachoutsLoader(Loader):
    source = "reachouts"

    def __init__(self, path: Path | None = None):
        self.path = Path(path or config.REACHOUTS_FILE)

    def load(self) -> list[RawRecord]:
        out: list[RawRecord] = []
        book = pd.read_excel(self.path, sheet_name=None, dtype=object)
        for sheet, df in book.items():
            missing = [c for c in EXPECTED if c not in df.columns]
            if missing:
                raise ValueError(f"{self.path.name}#{sheet}: missing columns {missing}")
            observed = config.REACHOUTS_SHEET_DATES.get(str(sheet))
            if observed is None:
                raise ValueError(f"{self.path.name}: no date configured for sheet {sheet!r}")
            stray = [c for c in df.columns if c not in EXPECTED]
            company_ffill = df["Company Name"].ffill()
            source_file = f"{self.path.name}#{sheet}"
            for i, row in df.iterrows():
                if all(cell(row[c]) is None for c in EXPECTED):
                    continue  # fully blank spacer row
                extra = {str(c): cell(row[c]) for c in stray if cell(row[c]) is not None}
                pieces = split_cell(row["Mail Address"]) or [None]
                for raw in pieces:
                    out.append(
                        RawRecord(
                            source=self.source,
                            source_file=source_file,
                            row_index=int(i) + 2,
                            email_raw=raw,
                            first_name_raw=cell(row["First Name"]),
                            company_raw=cell(company_ffill[i]),
                            linkedin_url=cell(row["Linkedin Address"]),
                            country=cell(row["Country"]),
                            category=cell(row["Category"]),
                            observed_at=observed,
                            observed_at_precision="year",
                            provenance_confirmed=config.SOURCE_PROVENANCE_CONFIRMED[self.source],
                            extra=extra,
                        )
                    )
        return out
