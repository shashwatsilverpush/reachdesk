from leadcore.normalize import nicknames
from leadcore.patterns.matcher import match_detail, matches


def test_expand_is_bidirectional():
    assert {"robert", "bob", "rob"} <= nicknames.expand("Robert")
    assert "robert" in nicknames.expand("bob")
    assert "nathan" in nicknames.expand("nate")


def test_expand_is_not_transitive():
    # robert->bill and william->bill both exist; robert must not reach william.
    assert "william" not in nicknames.expand("robert")


def test_bob_smith_matches_first_last_for_robert():
    assert matches("Robert", None, "bob.smith") == []  # without the nickname pass
    found = match_detail("Robert", "bob.smith", use_nicknames=True)
    assert [m.pattern for m in found] == ["first.last"]
    assert found[0].via_nickname and found[0].surname == "smith"


def test_exact_name_beats_nickname_reading():
    found = match_detail("Robert", "robert.smith", use_nicknames=True)
    assert [m.pattern for m in found] == ["first.last"]
    assert not found[0].via_nickname
