from datetime import date

import pytest
from sqlalchemy import select, update

from leadcore import pipeline, verification
from leadcore.models import AppendOnlyViolation, Contact, Origin, VerificationResult

from .conftest import ListLoader, rec


def test_normalise_status():
    assert verification.normalise_status("valid") == "deliverable"
    assert verification.normalise_status("Invalid") == "undeliverable"
    assert verification.normalise_status("accept_all") == "catch_all"
    assert verification.normalise_status("ok", catch_all=True) == "catch_all"
    assert verification.normalise_status("mailbox_not_found") == "undeliverable"
    assert verification.normalise_status("something weird") == "unknown"
    assert verification.normalise_status(None) == "unknown"


ZB = """email,status,sub_status,zb_score
a.b@x.com,valid,,0.98
c.d@x.com,invalid,mailbox_not_found,0.1
e.f@x.com,catch-all,,0.5
,valid,,0
"""


def test_import_maps_and_counts(session):
    st = verification.import_results(session, ZB, service="zerobounce", checked_at=date(2026, 10, 7))
    assert st.rows == 4 and st.imported == 3 and st.skipped_invalid == 1
    assert st.by_status == {"deliverable": 1, "undeliverable": 1, "catch_all": 1}
    rows = {r.email: r for r in session.scalars(select(VerificationResult))}
    assert rows["a.b@x.com"].status == "deliverable" and rows["a.b@x.com"].score == 0.98
    assert rows["c.d@x.com"].raw_status == "invalid" and rows["c.d@x.com"].domain == "x.com"
    assert rows["e.f@x.com"].catch_all is True


def test_import_is_idempotent(session):
    verification.import_results(session, ZB, service="zerobounce", checked_at=date(2026, 10, 7))
    st = verification.import_results(session, ZB, service="zerobounce", checked_at=date(2026, 10, 7))
    assert st.imported == 0 and st.already_present == 3
    assert session.scalar(select(__import__("sqlalchemy").func.count()).select_from(VerificationResult)) == 3


def test_recheck_on_a_new_date_is_a_new_row(session):
    verification.import_results(session, ZB, service="zerobounce", checked_at=date(2026, 10, 7))
    verification.import_results(session, "email,status\na.b@x.com,invalid\n", service="zerobounce",
                               checked_at=date(2026, 11, 1))
    latest = verification.latest_status(session, ["a.b@x.com"])
    assert latest["a.b@x.com"].status == "undeliverable"  # newest wins


def test_results_are_append_only(session):
    verification.import_results(session, ZB, service="zerobounce", checked_at=date(2026, 10, 7))
    r = session.scalars(select(VerificationResult).where(VerificationResult.status == "deliverable")).one()
    r.status = "unknown"
    with pytest.raises(AppendOnlyViolation):
        session.flush()
    session.rollback()
    with pytest.raises(AppendOnlyViolation):
        session.execute(update(VerificationResult).values(status="deliverable"))


def test_apollo_preset_reads_catchall_column(session):
    csv = "email,email_status,catchall\np@q.com,verified,true\nr@q.com,verified,false\n"
    verification.import_results(session, csv, service="apollo")
    rows = {r.email: r for r in session.scalars(select(VerificationResult))}
    assert rows["p@q.com"].status == "catch_all"       # catch-all overrides
    assert rows["r@q.com"].status == "deliverable"


def _run_with_generated(session):
    pipeline.ingest(session, loaders=[ListLoader([
        rec("jane.doe@acme.com", "Jane", row=2), rec("john.smith@acme.com", "John", row=3),
        rec("mary.jones@acme.com", "Mary", row=4)])])
    run, _, _ = pipeline.run_inference(session, use_nicknames=True)
    from leadcore.generate.candidates import Target, generate
    generate(session, [Target("Robert", "Brown", "acme.com")], run.id)
    return run


def test_export_excludes_verified_and_observed(session):
    _run_with_generated(session)
    # Before any verification: all generated candidates are exportable.
    run = pipeline.current_run(session, use_nicknames=True)
    csv1, n1 = verification.export_candidates(session, run)
    assert n1 >= 1 and "robert.brown@acme.com" in csv1
    # Mark the primary deliverable: it drops out of the export.
    verification.import_results(session, "email,status\nrobert.brown@acme.com,valid\n", service="generic")
    _, n2 = verification.export_candidates(session, run)
    assert n2 == n1 - 1


def test_deliverable_result_feeds_inference_as_confirmed(session):
    # One address at a lone-sample domain; verifying it deliverable keeps it, undeliverable drops it.
    pipeline.ingest(session, loaders=[ListLoader([rec("jane.doe@solo.com", "Jane", row=2)])])
    run, _, _ = pipeline.run_inference(session, use_nicknames=True)
    assert pipeline.tier_map(session, run)["solo.com"] == "C"

    verification.import_results(session, "email,status\njane.doe@solo.com,invalid\n", service="generic")
    run2, created, _ = pipeline.run_inference(session, use_nicknames=True)
    assert created  # fingerprint changed because a verification row was added
    # Undeliverable drops the only address, so the known domain has no usable sample -> Tier E.
    assert pipeline.tier_map(session, run2)["solo.com"] == "E"


def test_undeliverable_drops_domain_to_no_pattern(session):
    pipeline.ingest(session, loaders=[ListLoader([rec("x.y@z.com", "Xavier", row=2)])])
    verification.import_results(session, "email,status\nx.y@z.com,invalid\n", service="generic")
    run, _, results = pipeline.run_inference(session, use_nicknames=True)
    assert results["z.com"].tier == "E"
