"""Real-data checks. Slow (~1 min); opt in with LEADCORE_INTEGRATION=1."""

import os

import pytest
from sqlalchemy import func, select

from leadcore import config, pipeline
from leadcore.models import EmailObservation

pytestmark = pytest.mark.skipif(
    os.environ.get("LEADCORE_INTEGRATION") != "1" or not config.REACHOUTS_FILE.exists(),
    reason="set LEADCORE_INTEGRATION=1 with data/raw/ present",
)


def test_real_files_ingest_twice_idempotent(session):
    pipeline.ingest(session)
    run1, _, _ = pipeline.run_inference(session, use_nicknames=False)
    n1, tiers1 = session.scalar(select(func.count()).select_from(EmailObservation)), pipeline.tier_map(session, run1)

    again = pipeline.ingest(session)
    run2, created, _ = pipeline.run_inference(session, use_nicknames=False, rebuild=True)
    assert again.inserted == 0
    assert session.scalar(select(func.count()).select_from(EmailObservation)) == n1
    assert created and pipeline.tier_map(session, run2) == tiers1
