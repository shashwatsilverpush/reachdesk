import pytest

from leadcore.patterns import library
from leadcore.patterns.matcher import extract_surname, match_detail, matches

# (pattern, local for Jane Doe). One row per library pattern.
CASES = [(p.name, p.render("jane", "doe")) for p in library.PATTERNS]


def test_library_has_about_25_unique_patterns():
    assert 22 <= len(library.PATTERNS) <= 30
    rendered = [p.render("jane", "doe") for p in library.PATTERNS]
    assert len(rendered) == len(set(rendered)), "two patterns render identically"


def test_alias_firstlastinitial():
    assert library.get("firstlastinitial").name == "firstl"
    assert library.render("firstlastinitial", "jane", "doe") == "janed"


@pytest.mark.parametrize("pattern,local", CASES)
def test_known_surname_resolves_uniquely(pattern, local):
    assert matches("Jane", "Doe", local) == [pattern]


@pytest.mark.parametrize("pattern,local", [c for c in CASES if c[0] != "last"])
def test_residual_inference_resolves_uniquely(pattern, local):
    assert matches("Jane", None, local) == [pattern]


def test_last_alone_needs_known_surname():
    assert matches("Jane", None, "doe") == []
    assert matches("Jane", "Doe", "doe") == ["last"]


@pytest.mark.parametrize(
    "first,local,pattern,surname",
    [
        ("Saurabh", "saurabh.mahajan", "first.last", "mahajan"),
        ("Ted", "ttuttle", "flast", "tuttle"),
        ("Rob", "rob", "first", None),
        ("Nadine", "nadine.biber", "first.last", "biber"),
        ("Veit", "v.vetter", "f.last", "vetter"),
        ("Aisling", "aisling.mccabe", "first.last", "mccabe"),
        ("Maria", "maria.garcia-lopez", "first.last", "garcia-lopez"),
    ],
)
def test_residual_examples(first, local, pattern, surname):
    found = match_detail(first, local)
    assert [m.pattern for m in found] == [pattern]
    assert found[0].surname == surname
    assert found[0].surname_known is False


def test_unexplained_local():
    assert matches("David", None, "bigredmax") == []


def test_full_name_in_first_name_field_uses_known_surname():
    found = match_detail("Alex Mora", "alex.mora")
    assert [m.pattern for m in found] == ["first.last"]
    assert found[0].surname_known


def test_known_surname_disambiguates_flast_from_firstlast():
    # Without a surname, "tedwards" for Ted reads as firstlast + "wards".
    assert matches("Ted", None, "tedwards") == ["firstlast"]
    # With the real surname it is flast.
    assert matches("Ted", "Edwards", "tedwards") == ["flast"]


def test_extract_surname():
    assert extract_surname("Saurabh", "saurabh.mahajan", "first.last") == "mahajan"
    assert extract_surname("Ted", "ttuttle", "flast") == "tuttle"
    assert extract_surname("Rob", "rob", "first") is None
    assert extract_surname("Ted", "ttuttle", "first.last") is None


def test_matcher_ignores_case():
    assert matches("JANE", None, "Jane.Doe".lower()) == ["first.last"]
