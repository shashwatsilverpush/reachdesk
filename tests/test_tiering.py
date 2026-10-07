from datetime import date

import pytest

from leadcore import config
from leadcore.patterns.inference import ObservationView, Sample, infer, recency_weight, tier_domain

AS_OF = date(2026, 10, 7)
NEW, OLD, ANCIENT = date(2026, 1, 1), date(2025, 1, 1), date(2019, 1, 1)


def s(email, *patterns, when=NEW, weight=0.8, dated=True):
    return Sample(email, tuple(patterns), weight, when, dated, "own_correspondence")


def tier(samples):
    return tier_domain("x.com", samples, as_of=AS_OF)


def test_zero_usable_samples_is_E():
    assert tier([]).tier == "E"
    assert tier([s("a@x.com")]).tier == "E"  # unexplained only


def test_one_sample_is_C():
    r = tier([s("a@x.com", "first.last")])
    assert (r.tier, r.pattern, r.sample_count) == ("C", "first.last", 1)


def test_two_consistent_samples_is_B():
    r = tier([s("a@x.com", "first.last"), s("b@x.com", "first.last")])
    assert (r.tier, r.pattern) == ("B", "first.last")


def test_two_conflicting_samples_is_D():
    assert tier([s("a@x.com", "first.last"), s("b@x.com", "first", when=OLD, dated=False)]).tier == "D"


def test_three_consistent_recent_samples_is_A():
    r = tier([s(f"{i}@x.com", "flast") for i in "abc"])
    assert (r.tier, r.pattern, r.sample_count) == ("A", "flast", 3)


def test_three_samples_stale_is_B():
    r = tier([s(f"{i}@x.com", "flast", when=ANCIENT) for i in "abc"])
    assert r.tier == "B" and r.note == "dominant but stale"


def test_dominant_pattern_with_minor_dissent_is_A():
    samples = [s(f"{i}@x.com", "first.last") for i in "abcd"] + [s("e@x.com", "first")]
    r = tier(samples)
    assert r.tier == "A" and r.dominance == pytest.approx(0.8)


def test_genuine_conflict_stays_D():
    # Both patterns present in both years: no clean temporal split.
    samples = [s("a@x.com", "first.last", when=OLD), s("b@x.com", "first", when=OLD),
               s("c@x.com", "first.last", when=NEW), s("d@x.com", "first", when=NEW)]
    r = tier(samples)
    assert r.tier == "D" and r.migrated_from is None


def test_clean_temporal_split_downgrades_to_B():
    samples = [s("a@x.com", "first", when=OLD), s("b@x.com", "first", when=OLD),
               s("c@x.com", "first.last", when=NEW), s("d@x.com", "first.last", when=NEW)]
    r = tier(samples)
    assert (r.tier, r.pattern, r.migrated_from, r.note) == ("B", "first.last", "first", "convention migration")


def test_temporal_split_needs_evidence_dates():
    # The old-pattern samples carry an assumed date, so they cannot be placed before the cut.
    samples = [s("a@x.com", "first", when=OLD, dated=False), s("b@x.com", "first", when=OLD, dated=False),
               s("c@x.com", "first.last", when=NEW), s("d@x.com", "first.last", when=NEW)]
    assert tier(samples).tier == "D"


def test_unexplained_history_is_not_a_migration():
    samples = [s("a@x.com", when=OLD), s("b@x.com", when=OLD),
               s("c@x.com", "first.last", when=NEW), s("d@x.com", "first.last", when=NEW)]
    assert tier(samples).tier == "D"


def test_unexplained_counts_as_dissent(monkeypatch):
    samples = [s("a@x.com", "first.last"), s("b@x.com")]
    assert tier(samples).tier == "D"
    monkeypatch.setattr(config, "UNEXPLAINED_IS_DISSENT", False)
    assert tier(samples).tier == "C"


def test_recency_half_life():
    assert recency_weight(date(2023, 10, 7), AS_OF) == pytest.approx(0.5, abs=0.01)
    assert recency_weight(AS_OF, AS_OF) == 1.0


def _view(email, first, when=NEW, linkedin=None, confirmed=True):  # linkedin -> known_surname
    local, domain = email.split("@")
    return ObservationView(email, domain, local, first, linkedin, when, "year", confirmed)


def test_end_to_end_infer_with_sources_and_linkedin():
    views = [
        _view("nadine.biber@laola1.at", "Nadine", linkedin="biber"),
        _view("v.vetter@laola1.at", "Veit"),
        _view("d.horak@laola1.at", "Daniel"),
        _view("info@laola1.at", "there"),  # placeholder: ignored by inference
    ]
    r = infer(views, use_nicknames=False, as_of=AS_OF)["laola1.at"]
    # first.last (LinkedIn-confirmed, weight 1.0) vs two f.last (0.8 each): f.last leads at 0.62 share -> D
    assert r.tier == "D" and r.pattern == "f.last"
    sample = next(x for x in r.samples if x.email.startswith("nadine"))
    assert sample.source_class == "linkedin_confirmed" and sample.surname_source == "linkedin"


def test_source_confidence_ordering():
    w = config.SOURCE_WEIGHTS
    assert w["linkedin_confirmed"] > w["own_correspondence"] > w["own_database"] > w["unknown_provenance"]


def test_source_class_by_file():
    views = [_view("jane.doe@z.com", "Jane"),
             ObservationView("john.roe@z.com", "z.com", "john.roe", "John", None, NEW, "year", True,
                             source="leadlist"),
             ObservationView("ann.lee@z.com", "z.com", "ann.lee", "Ann", None, NEW, "year", False,
                             source="leadlist")]
    classes = {x.email: x.source_class for x in infer(views, use_nicknames=False, as_of=AS_OF)["z.com"].samples}
    assert classes == {"jane.doe@z.com": "own_correspondence", "john.roe@z.com": "own_database",
                       "ann.lee@z.com": "unknown_provenance"}


def test_known_surname_blocks_invented_residual_surname():
    # "phu" for Phuong Doan would read as flast with surname "hu": rejected.
    views = [ObservationView("phu@v.com", "v.com", "phu", "Phuong", "Doan", NEW, "day", True,
                             surname_from_provider=True)]
    [sample] = infer(views, use_nicknames=False, as_of=AS_OF)["v.com"].samples
    assert sample.patterns == ()


def test_known_surname_allows_transliteration_and_truncation():
    views = [ObservationView("abdallah.khalifh@r.tv", "r.tv", "abdallah.khalifh", "Abdallah", "Khalifeh",
                             NEW, "day", True, surname_from_provider=True),
             ObservationView("shahar.st@b.com", "b.com", "shahar.st", "Shahar", "Stein", NEW, "day", True,
                             surname_from_provider=True)]
    res = infer(views, use_nicknames=False, as_of=AS_OF)
    assert res["r.tv"].samples[0].patterns == ("first.last",)
    assert res["b.com"].samples[0].patterns == ("first.last",)


def test_slug_surname_does_not_block_the_address_reading():
    views = [ObservationView("justine.rigby@o.com", "o.com", "justine.rigby", "Justine", "robinsonsnapshot",
                             NEW, "year", True)]
    assert infer(views, use_nicknames=False, as_of=AS_OF)["o.com"].samples[0].patterns == ("first.last",)


@pytest.mark.parametrize("pattern,sub", [("first.last", "C+"), ("lastfirst", "C+"), ("firstlast", "C+"),
                                         ("first", "C-"), ("flast", "C-"), ("f.last", "C-"), ("firstl", "C-")])
def test_tier_c_split_by_shape_of_the_single_address(pattern, sub):
    r = tier([s("a@x.com", pattern)])
    assert (r.tier, r.subtier) == ("C", sub)


def test_only_tier_c_has_a_subtier():
    assert tier([s("a@x.com", "first.last"), s("b@x.com", "first.last")]).subtier is None
