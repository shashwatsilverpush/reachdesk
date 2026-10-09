"""Tests for Stage 1 — syntax validation."""

from email_verifier.syntax import check_syntax


class TestMalformed:
    def test_no_at_sign(self) -> None:
        r = check_syntax("noatsign")
        assert not r.valid
        assert r.reason == "syntax"

    def test_double_at(self) -> None:
        r = check_syntax("a@@b.com")
        assert not r.valid

    def test_empty_string(self) -> None:
        r = check_syntax("")
        assert not r.valid

    def test_spaces(self) -> None:
        r = check_syntax("user @example.com")
        assert not r.valid


class TestLengthLimits:
    def test_local_too_long(self) -> None:
        local = "a" * 65
        r = check_syntax(f"{local}@example.com")
        assert not r.valid
        assert r.reason == "syntax"

    def test_domain_too_long(self) -> None:
        domain = "a" * 254 + ".com"
        r = check_syntax(f"user@{domain}")
        assert not r.valid

    def test_total_too_long(self) -> None:
        local = "a" * 64
        domain = "b" * 186 + ".com"
        email = f"{local}@{domain}"
        assert len(email) > 254
        r = check_syntax(email)
        assert not r.valid


class TestDots:
    def test_double_dot_in_local(self) -> None:
        r = check_syntax("bad..address@gmail.com")
        assert not r.valid
        assert r.reason == "syntax"

    def test_double_dot_in_domain(self) -> None:
        r = check_syntax("user@bad..domain.com")
        assert not r.valid

    def test_leading_dot_in_local(self) -> None:
        r = check_syntax(".user@example.com")
        assert not r.valid

    def test_trailing_dot_in_local(self) -> None:
        r = check_syntax("user.@example.com")
        assert not r.valid


class TestNormalization:
    def test_lowercase(self) -> None:
        r = check_syntax("USER@Example.COM")
        assert r.valid
        assert r.email == "user@example.com"

    def test_idn_to_punycode(self) -> None:
        r = check_syntax("user@münchen.de")
        assert r.valid
        assert r.domain == "xn--mnchen-3ya.de"

    def test_whitespace_stripped(self) -> None:
        r = check_syntax("  user@example.com  ")
        assert r.valid
        assert r.email == "user@example.com"


class TestFlags:
    def test_role_account(self) -> None:
        r = check_syntax("admin@example.com")
        assert r.valid
        assert "role" in r.flags

    def test_disposable(self) -> None:
        r = check_syntax("user@mailinator.com")
        assert r.valid
        assert "disposable" in r.flags

    def test_free_provider(self) -> None:
        r = check_syntax("user@gmail.com")
        assert r.valid
        assert "free" in r.flags

    def test_no_flags_corporate(self) -> None:
        r = check_syntax("alice@acme-corp.com")
        assert r.valid
        assert r.flags == []


class TestTypoSuggestion:
    def test_gmial_suggests_gmail(self) -> None:
        r = check_syntax("user@gmial.com")
        assert r.valid
        assert r.suggestion == "user@gmail.com"

    def test_hotmal_suggests_hotmail(self) -> None:
        r = check_syntax("user@hotmal.com")
        assert r.valid
        assert r.suggestion == "user@hotmail.com"

    def test_no_suggestion_for_known_domain(self) -> None:
        r = check_syntax("user@gmail.com")
        assert r.suggestion is None

    def test_no_suggestion_for_distant_domain(self) -> None:
        r = check_syntax("user@totally-custom-corp.com")
        assert r.suggestion is None


class TestValidEmails:
    def test_simple(self) -> None:
        r = check_syntax("alice@example.com")
        assert r.valid
        assert r.local == "alice"
        assert r.domain == "example.com"

    def test_with_dots(self) -> None:
        r = check_syntax("first.last@example.com")
        assert r.valid

    def test_with_plus(self) -> None:
        r = check_syntax("user+tag@example.com")
        assert r.valid
