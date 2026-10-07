"""Loader for Lead_list.xlsx (single sheet: firstName, email, companyName).

``companyName`` is sometimes a domain and sometimes a display name
("Mind Tank Media"); it is kept verbatim. The file carries no dates, so rows
are dated to ``config.LEADLIST_ASSUMED_DATE`` with precision "assumed".
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from leadcore import config
from leadcore.ingest.base import Loader, RawRecord, cell
from leadcore.normalize.emails import split_cell

EXPECTED = ["firstName", "email", "companyName"]


class LeadListLoader(Loader):
    source = "leadlist"

    def __init__(self, path: Path | None = None):
        self.path = Path(path or config.LEADLIST_FILE)

    def load(self) -> list[RawRecord]:
        df = pd.read_excel(self.path, dtype=object)
        missing = [c for c in EXPECTED if c not in df.columns]
        if missing:
            raise ValueError(f"{self.path.name}: missing columns {missing}")
        stray = [c for c in df.columns if c not in EXPECTED]
        out: list[RawRecord] = []
        for i, row in df.iterrows():
            if all(cell(row[c]) is None for c in EXPECTED):
                continue
            extra = {str(c): cell(row[c]) for c in stray if cell(row[c]) is not None}
            for raw in split_cell(row["email"]) or [None]:
                out.append(
                    RawRecord(
                        source=self.source,
                        source_file=self.path.name,
                        row_index=int(i) + 2,
                        email_raw=raw,
                        first_name_raw=cell(row["firstName"]),
                        company_raw=cell(row["companyName"]),
                        observed_at=config.LEADLIST_ASSUMED_DATE,
                        observed_at_precision="assumed",
                        provenance_confirmed=config.SOURCE_PROVENANCE_CONFIRMED[self.source],
                        extra=extra,
                    )
                )
        return out
