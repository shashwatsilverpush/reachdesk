from datetime import date

from sqlalchemy import select

from leadcore import pipeline
from leadcore.ingest.apollo import ApolloLoader
from leadcore.models import Contact, EmailObservation

CSV = """email,first_name,last_name,linkedin_url,organization_domain,email_status,catchall,revealed_at
keon.ko@x.com,Keon,Ko,,x.com,verified,false,2026-10-07
tserge@y.com,Tony,S,,y.com,verified,true,2026-10-07
"""


def _load(tmp_path):
    p = tmp_path / "apollo_verified.csv"
    p.write_text(CSV)
    return ApolloLoader(p)


def test_apollo_loader_fields(tmp_path):
    a, b = _load(tmp_path).load()
    assert (a.source, a.observed_at, a.observed_at_precision) == ("apollo", date(2026, 10, 7), "day")
    assert a.last_name_raw == "Ko" and a.delivery_confirmed and a.provenance_confirmed
    assert not b.delivery_confirmed  # catch-all: "verified" proves nothing
    assert b.extra == {"email_status": "verified", "catchall": True}


def test_missing_file_loads_nothing(tmp_path):
    assert ApolloLoader(tmp_path / "nope.csv").load() == []


def test_apollo_surname_is_known_and_masked_one_is_not(session, tmp_path):
    pipeline.ingest(session, loaders=[_load(tmp_path)])
    c = {c.email: c for c in session.scalars(select(Contact))}
    assert (c["keon.ko@x.com"].last_name, c["keon.ko@x.com"].last_name_source) == ("Ko", "apollo")
    assert c["tserge@y.com"].last_name is None
    obs = session.scalars(select(EmailObservation).where(EmailObservation.email == "keon.ko@x.com")).one()
    assert obs.delivery_confirmed and obs.last_name_raw == "Ko"

    _, _, results = pipeline.run_inference(session, use_nicknames=True)
    [sample] = results["x.com"].samples
    # Known surname resolves keonko-style ambiguity and earns the confirmed-surname class.
    assert sample.patterns == ("first.last",) and sample.source_class == "linkedin_confirmed"
