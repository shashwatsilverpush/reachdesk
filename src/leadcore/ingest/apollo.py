"""Loader for verified addresses revealed through Apollo (data/raw/apollo_verified.csv).

Columns: email, first_name, last_name, linkedin_url, organization_domain,
email_status, catchall, revealed_at.

Only rows Apollo marks ``verified`` are delivery-confirmed, and only when the
domain is not catch-all: a catch-all server accepts any address, so
"verified" there proves nothing about this one. Surnames masked to an initial
("Tony S") are kept verbatim but not used as known surnames.
"""

from __future__ import annotations

import csv
from datetime import date
from pathlib import Path

from leadcore import config
from leadcore.ingest.base import Loader, RawRecord


class ApolloLoader(Loader):
    source = "apollo"

    def __init__(self, path: Path | None = None):
        self.path = Path(path or config.APOLLO_FILE)

    def load(self) -> list[RawRecord]:
        if not self.path.exists():
            return []
        out = []
        with open(self.path, newline="", encoding="utf-8") as fh:
            for i, row in enumerate(csv.DictReader(fh), start=2):
                verified = row.get("email_status", "").lower() == "verified"
                catchall = row.get("catchall", "").lower() == "true"
                out.append(RawRecord(
                    source=self.source,
                    source_file=self.path.name,
                    row_index=i,
                    email_raw=row.get("email") or None,
                    first_name_raw=row.get("first_name") or None,
                    last_name_raw=row.get("last_name") or None,
                    linkedin_url=row.get("linkedin_url") or None,
                    company_raw=row.get("organization_domain") or None,
                    observed_at=date.fromisoformat(row["revealed_at"]),
                    observed_at_precision="day",
                    provenance_confirmed=config.SOURCE_PROVENANCE_CONFIRMED[self.source],
                    delivery_confirmed=verified and not catchall,
                    extra={"email_status": row.get("email_status"), "catchall": catchall},
                ))
        return out
