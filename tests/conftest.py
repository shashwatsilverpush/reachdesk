from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from leadcore import db
from leadcore.ingest.base import Loader, RawRecord


@pytest.fixture
def db_url(tmp_path) -> str:
    url = f"sqlite:///{tmp_path / 'test.db'}"
    db.migrate(url)  # exercise the real Alembic migration, not create_all
    return url


@pytest.fixture
def session(db_url):
    with db.session(db_url) as s:
        yield s


class ListLoader(Loader):
    """Loader over in-memory records, for tests."""

    def __init__(self, records: list[RawRecord], source: str = "test"):
        self.records, self.source = records, source

    def load(self) -> list[RawRecord]:
        return list(self.records)


def rec(email, first, *, row, source="reachouts", file="Reachouts_.xlsx#2026",
        when=date(2026, 1, 1), confirmed=True, linkedin=None, precision="year") -> RawRecord:
    return RawRecord(source=source, source_file=file, row_index=row, email_raw=email,
                     first_name_raw=first, linkedin_url=linkedin, observed_at=when,
                     observed_at_precision=precision, provenance_confirmed=confirmed)


@pytest.fixture
def mini_xlsx(tmp_path) -> tuple[Path, Path]:
    """Small workbooks with the real files' shapes and quirks."""
    reach = tmp_path / "Reachouts_.xlsx"
    with pd.ExcelWriter(reach) as xw:
        pd.DataFrame({
            "Company Name": ["laola1.tv", None, None, "acme.com", None],
            "First Name": ["Nadine", "Veit", "there", "Jane", "Bob"],
            "Mail Address": ["nadine.biber@laola1.at", "v.vetter@laola1.at", "info@laola1.at",
                             "jane.doe@acme.com", "bob@gmail.com"],
            "Linkedin Address": ["https://www.linkedin.com/in/nadine-biber-801810116/", None, None, None, None],
            "Country": ["Austria", None, None, "USA", None],
            "Category": ["Aggregator", None, None, None, None],
            "Unnamed: 6": [None, None, None, "lea_abc", None],
        }).to_excel(xw, sheet_name="2025", index=False)
        pd.DataFrame({
            "Company Name": ["acme.com", None],
            "First Name": ["John", "Mary"],
            "Mail Address": ["john.smith@acme.com", "contact out"],
            "Linkedin Address": [None, None],
            "Country": [None, None],
            "Category": [None, None],
        }).to_excel(xw, sheet_name="2026", index=False)
    lead = tmp_path / "Lead_list.xlsx"
    pd.DataFrame({
        "firstName": ["there", "Jane", None, "team"],
        "email": ["advertising@acme.com", "jane.doe@acme.com", "alex.lee@acme.com", "hello@acme.com"],
        "companyName": ["acme.com", "Acme Inc", None, None],
    }).to_excel(lead, index=False)
    return reach, lead
