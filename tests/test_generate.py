from collections import Counter

from sqlalchemy import select

from leadcore import pipeline
from leadcore.generate.candidates import Target, candidates_for, generate
from leadcore.models import CanaryStatus, Contact, DomainPattern, GenerationFlag, Origin

from .conftest import ListLoader, rec


def _setup(session, records):
    pipeline.ingest(session, loaders=[ListLoader(records)])
    run, _, _ = pipeline.run_inference(session, use_nicknames=True)
    return run


FIRST_LAST_DOMAIN = [
    rec("jane.doe@acme.com", "Jane", row=2),
    rec("john.smith@acme.com", "John", row=3),
    rec("mary.jones@acme.com", "Mary", row=4),
]


def test_primary_and_nickname_candidates():
    cands = candidates_for(Target("Robert", "Smith", "acme.com"), "first.last",
                           name_frequency=Counter({"bob": 10, "rob": 5}))
    addrs = [c.address for c in cands]
    assert addrs[0] == "robert.smith@acme.com" and cands[0].kind == "primary"
    assert "bob.smith@acme.com" in addrs and addrs.index("bob.smith@acme.com") == 1
    assert all(c.kind == "nickname" for c in cands[1:])


def test_candidate_normalises_target_name():
    [c, *_] = candidates_for(Target("José", "van der Berg", "acme.com"), "flast")
    assert c.address == "jvanderberg@acme.com"


def test_generation_emits_generated_pending_contacts(session):
    run = _setup(session, FIRST_LAST_DOMAIN)
    [res] = generate(session, [Target("Robert", "Brown", "acme.com")], run.id)
    assert res.status == "generated"
    rows = session.scalars(select(Contact).where(Contact.origin == Origin.generated)).all()
    assert {r.email for r in rows} >= {"robert.brown@acme.com", "bob.brown@acme.com"}
    dp = session.scalars(select(DomainPattern).where(DomainPattern.run_id == run.id,
                                                     DomainPattern.domain == "acme.com")).one()
    assert all(r.canary_status == CanaryStatus.pending and r.derived_from_pattern_id == dp.id for r in rows)
    # Generated-pending never appears in the sendable set.
    assert not {c.email for c in session.scalars(Contact.sendable())} & {r.email for r in rows}


def test_cross_collision_blocks_generation(session):
    # A different person (Jonathan) already holds j.smith-style address under flast.
    run = _setup(session, [
        rec("jsmith@corp.com", "Jonathan", row=2),
        rec("mdoe@corp.com", "Mary", row=3),
        rec("pjones@corp.com", "Paul", row=4),
    ])
    [res] = generate(session, [Target("Julia", "Smith", "corp.com")], run.id)
    assert res.status == "blocked" and res.reason == "cross-collision"
    assert not session.scalars(select(Contact).where(Contact.origin == Origin.generated)).all()
    flag = session.scalars(select(GenerationFlag)).one()
    assert (flag.kind, flag.address, flag.target_name, flag.other_name) == \
        ("cross", "jsmith@corp.com", "Julia Smith", "Jonathan")
    assert flag.observation_id is not None


def test_same_person_is_not_a_collision(session):
    run = _setup(session, FIRST_LAST_DOMAIN)
    [res] = generate(session, [Target("Jane", "Doe", "acme.com")], run.id)
    assert res.status == "already_held"
    assert not session.scalars(select(GenerationFlag)).all()


def test_self_collision_blocks_both(session):
    run = _setup(session, [
        rec("jsmith@corp.com", "Jonathan", row=2),
        rec("mdoe@corp.com", "Mary", row=3),
        rec("pjones@corp.com", "Paul", row=4),
    ])
    targets = [Target("Kate", "Brown", "corp.com"), Target("Kevin", "Brown", "corp.com"),
               Target("Lucy", "Gray", "corp.com")]
    res = {r.target.first: r for r in generate(session, targets, run.id)}
    assert res["Kate"].status == res["Kevin"].status == "blocked"
    assert res["Kate"].reason == "self-collision"
    assert res["Lucy"].status == "generated"
    emails = {c.email for c in session.scalars(select(Contact).where(Contact.origin == Origin.generated))}
    assert "kbrown@corp.com" not in emails and "lgray@corp.com" in emails
    assert {f.target_name for f in session.scalars(select(GenerationFlag))} == {"Kate Brown", "Kevin Brown"}


def test_tier_d_and_e_are_not_generatable(session):
    run = _setup(session, [
        rec("jane.doe@mixed.com", "Jane", row=2),
        rec("john@mixed.com", "John", row=3),
    ])
    [res] = generate(session, [Target("Ann", "Lee", "mixed.com")], run.id)
    assert res.status == "not_generatable" and "tier D" in res.reason
    [res] = generate(session, [Target("Ann", "Lee", "unknown.com")], run.id)
    assert res.status == "not_generatable"
