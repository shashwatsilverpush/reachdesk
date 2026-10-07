import pytest

from leadcore.normalize import emails
from leadcore.normalize.names import fold, given_variants, parse_display_name, surname_variants


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("José", "jose"),
        ("Narváez", "narvaez"),
        ("Müller", "muller"),
        ("Søren", "soren"),
        ("Łukasz", "lukasz"),
        ("Straße", "strasse"),
        ("Ærø", "aero"),
        ("  François  ", "francois"),
    ],
)
def test_accents(raw, expected):
    assert fold(raw) == expected


def test_transliteration_non_latin():
    assert fold("Дмитрий") == "dmitrii"
    assert fold("Γιώργος").startswith("giorg")
    assert fold("राहुल").isascii() and fold("राहुल")


def test_hyphenated_surname():
    v = surname_variants("Smith-Jones")
    assert v[0] == "smithjones"
    assert {"smithjones", "smith-jones", "smith", "jones"} <= set(v)


def test_van_der():
    v = surname_variants("van der Berg")
    assert v[0] == "vanderberg"
    assert {"vanderberg", "vandenberg", "berg", "vberg"} <= set(v)


def test_apostrophe_surname():
    assert {"obrien", "brien"} <= set(surname_variants("O'Brien"))


def test_hyphenated_given_name():
    assert {"jeanpierre", "jean-pierre", "jean"} <= set(given_variants("Jean-Pierre"))


def test_single_name_record():
    interps = parse_display_name("Saurabh")
    assert len(interps) == 1
    assert interps[0].first == "saurabh" and interps[0].last is None


def test_full_name_split():
    i = parse_display_name("John Smith")[0]
    assert (i.first, i.last) == ("john", "smith")


def test_middle_initial():
    i = parse_display_name("John Q. Smith")[0]
    assert (i.first, i.last, i.middle) == ("john", "smith", ("q",))


def test_particle_surname_from_display_name():
    i = parse_display_name("Jan van der Berg")[0]
    assert (i.first, i.last) == ("jan", "van der berg")


def test_indian_expanded_initials():
    readings = {(i.first, i.last) for i in parse_display_name("R.K. Sharma")}
    assert ("sharma", "rk") in readings      # sharma.rk / sharmar...
    assert ("r", "sharma") in readings       # rsharma / r.sharma
    readings = {(i.first, i.last) for i in parse_display_name("K. Ramesh")}
    assert ("ramesh", "k") in readings       # ramesh.k


def test_surname_first_ordering():
    readings = [(i.first, i.last, i.reading) for i in parse_display_name("Sharma Rahul")]
    assert ("rahul", "sharma", "surname_first") in readings


def test_indian_initials_match_end_to_end():
    from leadcore.patterns.matcher import match_detail
    assert [m.pattern for m in match_detail("K. Ramesh", "ramesh.k")] == ["first.l"]
    assert [m.pattern for m in match_detail("K. Ramesh", "kramesh")] == ["lfirst"]
    # Name order cannot be read off an address: the as-given reading wins ties...
    assert [m.pattern for m in match_detail("Sharma Rahul", "rahul.sharma")] == ["last.first"]
    # Templates are symmetric under a first/last swap, so in matching the
    # surname-first reading can only tie the as-given one; its value is in
    # generation (see test_generate). Here "rahul" is `last` under the as-given reading.
    assert [m.pattern for m in match_detail("Sharma Rahul", "rahul")] == ["last"]


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("  Jane.Doe@Example.COM ", "jane.doe@example.com"),
        ("priscilla.ramos@tvn .cl", "priscilla.ramos@tvn.cl"),
        ("trueid.intrend@ truedigital.com", "trueid.intrend@truedigital.com"),
        ("mailto:a@b.co", "a@b.co"),
        ("<a@b.co>", "a@b.co"),
        ("contact out", None),
        ("CO", None),
        ("Pierre", None),
        ("a..b@c.com", None),
    ],
)
def test_canonicalise(raw, expected):
    assert emails.canonicalise(raw) == expected


def test_parse_strips_tag_and_digits():
    p = emails.parse("john.smith2+news@acme.com")
    assert p.match_local == "john.smith" and p.numeric_suffix == "2" and p.domain == "acme.com"


def test_split_cell():
    assert emails.split_cell("a@x.com; b@x.com") == ["a@x.com", "b@x.com"]
    assert emails.split_cell("priscilla.ramos@tvn .cl") == ["priscilla.ramos@tvn .cl"]
