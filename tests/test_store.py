"""Persistence-level guarantees: idempotency, provenance, append-only, exclusions."""

from datetime import date

import pytest
from sqlalchemy import delete, func, select, update

from leadcore import pipeline
from leadcore.ingest.leadlist import LeadListLoader
from leadcore.ingest.reachouts import ReachoutsLoader
from leadcore.models import (AppendOnlyViolation, CanaryStatus, Contact, DomainPattern, EmailObservation,
                             InferenceRun, Origin)

from .conftest import ListLoader, rec


def _loaders(mini_xlsx):
    reach, lead = mini_xlsx
    return [ReachoutsLoader(reach), LeadListLoader(lead)]


def _count(session, model):
    return session.scalar(select(func.count()).select_from(model))


# --- Idempotency --------------------------------------------------------------------


def test_ingest_twice_is_idempotent(session, mini_xlsx):
    first = pipeline.ingest(session, loaders=_loaders(mini_xlsx))
    run1, created1, _ = pipeline.run_inference(session, use_nicknames=True)
    tiers1 = pipeline.tier_map(session, run1)
    n_obs, n_contacts = _count(session, EmailObservation), _count(session, Contact)

    second = pipeline.ingest(session, loaders=_loaders(mini_xlsx))
    run2, created2, _ = pipeline.run_inference(session, use_nicknames=True)

    assert first.inserted == n_obs and second.inserted == 0 and second.already_present == n_obs
    assert _count(session, EmailObservation) == n_obs
    assert _count(session, Contact) == n_contacts
    assert created1 and not created2 and run2.id == run1.id
    assert pipeline.tier_map(session, run2) == tiers1


def test_rebuild_produces_identical_tiers(session, mini_xlsx):
    pipeline.ingest(session, loaders=_loaders(mini_xlsx))
    run1, _, _ = pipeline.run_inference(session, use_nicknames=True)
    run2, created, _ = pipeline.run_inference(session, use_nicknames=True, rebuild=True)
    assert created and run2.id != run1.id
    assert pipeline.tier_map(session, run2) == pipeline.tier_map(session, run1)


def test_obs_key_distinguishes_rows_and_files(session):
    loader = ListLoader([rec("a.b@x.com", "A", row=2), rec("a.b@x.com", "A", row=3),
                         rec("a.b@x.com", "A", row=2, file="Reachouts_.xlsx#2025")])
    assert pipeline.ingest(session, loaders=[loader]).inserted == 3
    assert _count(session, Contact) == 1  # dedup on lowercased email


# --- Loader quirks ------------------------------------------------------------------


def test_reachouts_ffill_and_stated_domain(session, mini_xlsx):
    pipeline.ingest(session, loaders=_loaders(mini_xlsx))
    veit = session.scalars(select(EmailObservation).where(EmailObservation.email == "v.vetter@laola1.at")).one()
    assert veit.company_name_raw == "laola1.tv"          # forward-filled
    assert veit.stated_domain == "laola1.tv"             # kept separately...
    assert veit.domain == "laola1.at"                    # ...but keyed on the email domain
    assert veit.source_file == "Reachouts_.xlsx#2025" and veit.observed_at == date(2025, 1, 1)
    jane = session.scalars(select(EmailObservation).where(EmailObservation.email == "jane.doe@acme.com",
                                                          EmailObservation.source == "reachouts")).one()
    assert jane.extra == {"Unnamed: 6": "lea_abc"}
    nadine = session.scalars(select(EmailObservation).where(EmailObservation.email.like("nadine%"))).one()
    assert nadine.linkedin_surname == "biber"


# --- Provenance -----------------------------------------------------------------------


def _sendable(session):
    return set(session.scalars(Contact.sendable()).all())


def test_sendable_excludes_unconfirmed_provenance_and_pending_generated(session):
    pipeline.ingest(session, loaders=[ListLoader([
        rec("own.person@x.com", "Own", row=2, confirmed=True),
        rec("bought.person@x.com", "Bought", row=3, source="leadlist", file="Lead_list.xlsx",
            confirmed=False, precision="assumed"),
    ])])
    run, _, _ = pipeline.run_inference(session, use_nicknames=True)
    dp = session.scalars(select(DomainPattern).where(DomainPattern.run_id == run.id)).one()
    gen_pending = Contact(email="new.person@x.com", domain="x.com", origin=Origin.generated,
                          provenance_confirmed=True, derived_from_pattern_id=dp.id)
    gen_passed = Contact(email="ok.person@x.com", domain="x.com", origin=Origin.generated,
                         provenance_confirmed=True, derived_from_pattern_id=dp.id,
                         canary_status=CanaryStatus.passed)
    session.add_all([gen_pending, gen_passed])
    session.flush()
    assert gen_pending.canary_status == CanaryStatus.pending  # default

    emails = {c.email for c in _sendable(session)}
    assert emails == {"own.person@x.com", "ok.person@x.com"}
    bought = session.scalars(select(Contact).where(Contact.email == "bought.person@x.com")).one()
    assert bought.provenance_confirmed is False and bought.origin == Origin.confirmed


def test_unconfirmed_provenance_still_seeds_inference(session):
    pipeline.ingest(session, loaders=[ListLoader([
        rec("jane.doe@y.com", "Jane", row=2, source="leadlist", file="Lead_list.xlsx", confirmed=False),
    ])])
    run, _, _ = pipeline.run_inference(session, use_nicknames=True)
    assert pipeline.tier_map(session, run) == {"y.com": "C"}
    assert _sendable(session) == set()


def test_generated_contact_requires_pattern(session):
    session.add(Contact(email="a@x.com", domain="x.com", origin=Origin.generated, provenance_confirmed=True))
    with pytest.raises(ValueError):
        session.flush()
    session.rollback()


def test_confirmed_contact_cannot_carry_generation_fields(session):
    session.add(Contact(email="a@x.com", domain="x.com", origin=Origin.confirmed, provenance_confirmed=True,
                        canary_status=CanaryStatus.pending))
    with pytest.raises(ValueError):
        session.flush()
    session.rollback()


# --- Append-only ------------------------------------------------------------------------


def test_observations_are_append_only(session, mini_xlsx):
    pipeline.ingest(session, loaders=_loaders(mini_xlsx))
    session.commit()
    o = session.scalars(select(EmailObservation).limit(1)).one()
    o.first_name_raw = "Changed"
    with pytest.raises(AppendOnlyViolation):
        session.flush()
    session.rollback()
    o = session.scalars(select(EmailObservation).limit(1)).one()
    session.delete(o)
    with pytest.raises(AppendOnlyViolation):
        session.flush()
    session.rollback()
    with pytest.raises(AppendOnlyViolation):
        session.execute(update(EmailObservation).values(first_name_raw="x"))
    with pytest.raises(AppendOnlyViolation):
        session.execute(delete(EmailObservation))
    session.rollback()
    assert _count(session, EmailObservation) > 0


# --- Exclusions -------------------------------------------------------------------------


def test_exclusions_are_stored_but_not_inferred(session, mini_xlsx):
    pipeline.ingest(session, loaders=_loaders(mini_xlsx))
    reasons = dict(session.execute(select(EmailObservation.email_raw, EmailObservation.excluded_reason)).all())
    assert reasons["info@laola1.at"] == "role_address"
    assert reasons["bob@gmail.com"] == "consumer_domain"
    assert reasons["contact out"] == "invalid_email"
    assert reasons["advertising@acme.com"] == "role_address"
    assert reasons["alex.lee@acme.com"] == "placeholder_name"

    run, _, results = pipeline.run_inference(session, use_nicknames=True)
    acme = results["acme.com"]
    assert {x.email for x in acme.samples} == {"jane.doe@acme.com", "john.smith@acme.com"}
    assert "gmail.com" not in results

    # Placeholder-named addresses are still business contacts; role/consumer/invalid are not.
    contacts = set(session.scalars(select(Contact.email)))
    assert "alex.lee@acme.com" in contacts
    assert not contacts & {"info@laola1.at", "bob@gmail.com", "advertising@acme.com", "hello@acme.com"}


def test_inference_run_records_config(session, mini_xlsx):
    pipeline.ingest(session, loaders=_loaders(mini_xlsx))
    run, _, _ = pipeline.run_inference(session, use_nicknames=False)
    stored = session.get(InferenceRun, run.id)
    assert stored.config["half_life"] == 3.0 and "engine_version" in stored.config
