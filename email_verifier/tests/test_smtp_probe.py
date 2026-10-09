"""Tests for Stages 3 & 4 — SMTP probe and catch-all detection."""

from __future__ import annotations

from email_verifier.smtp_probe import classify, is_catch_all, smtp_check, smtp_rcpt

HELO = "test.local"
SENDER = "test@verify.org"


class TestClassify:
    def test_250_valid(self) -> None:
        assert classify(250) == "valid"

    def test_251_valid(self) -> None:
        assert classify(251) == "valid"

    def test_550_invalid(self) -> None:
        assert classify(550) == "invalid"

    def test_551_invalid(self) -> None:
        assert classify(551) == "invalid"

    def test_553_invalid(self) -> None:
        assert classify(553) == "invalid"

    def test_552_valid_full(self) -> None:
        assert classify(552) == "valid"

    def test_450_unknown(self) -> None:
        assert classify(450) == "unknown"

    def test_252_unknown(self) -> None:
        assert classify(252) == "unknown"

    def test_421_unknown(self) -> None:
        assert classify(421) == "unknown"

    def test_0_unknown(self) -> None:
        assert classify(0) == "unknown"


class TestSmtpRcpt:
    def test_truthful_exists(self, truthful_server: tuple[str, int]) -> None:
        host, port = truthful_server
        code, msg = smtp_rcpt(
            "exists@test.com", host, SENDER, helo=HELO, timeout=5, port=port
        )
        assert code == 250
        assert "OK" in msg

    def test_truthful_not_exists(self, truthful_server: tuple[str, int]) -> None:
        host, port = truthful_server
        code, msg = smtp_rcpt(
            "nobody@test.com", host, SENDER, helo=HELO, timeout=5, port=port
        )
        assert code == 550

    def test_catchall_accepts_anything(self, catchall_server: tuple[str, int]) -> None:
        host, port = catchall_server
        code, _ = smtp_rcpt(
            "random@test.com", host, SENDER, helo=HELO, timeout=5, port=port
        )
        assert code == 250

    def test_greylist_first_attempt(self, greylist_server: tuple[str, int]) -> None:
        host, port = greylist_server
        code, _ = smtp_rcpt(
            "user@test.com", host, SENDER, helo=HELO, timeout=5, port=port
        )
        assert code == 450

    def test_greylist_second_attempt(self, greylist_server: tuple[str, int]) -> None:
        host, port = greylist_server
        smtp_rcpt("user@test.com", host, SENDER, helo=HELO, timeout=5, port=port)
        code, _ = smtp_rcpt(
            "user@test.com", host, SENDER, helo=HELO, timeout=5, port=port
        )
        assert code == 250

    def test_full_mailbox(self, full_server: tuple[str, int]) -> None:
        host, port = full_server
        code, msg = smtp_rcpt(
            "anyone@test.com", host, SENDER, helo=HELO, timeout=5, port=port
        )
        assert code == 552
        assert "full" in msg.lower()


class TestCatchAll:
    def test_not_catch_all(self, truthful_server: tuple[str, int]) -> None:
        host, port = truthful_server
        result = is_catch_all(
            "test.com", host, SENDER, helo=HELO, timeout=5, port=port
        )
        assert result is False

    def test_catch_all(self, catchall_server: tuple[str, int]) -> None:
        host, port = catchall_server
        result = is_catch_all(
            "test.com", host, SENDER, helo=HELO, timeout=5, port=port
        )
        assert result is True

    def test_apple_like_probe_fails(self, apple_like: tuple[str, int]) -> None:
        """First RCPT succeeds, subsequent ones get 421 -> None."""
        host, port = apple_like
        code, _ = smtp_rcpt(
            "target@test.com", host, SENDER, helo=HELO, timeout=5, port=port
        )
        assert code == 250
        result = is_catch_all(
            "test.com", host, SENDER, helo=HELO, timeout=5, port=port
        )
        assert result is None


class TestSmtpCheck:
    def test_valid_not_catchall(self, truthful_server: tuple[str, int]) -> None:
        host, port = truthful_server
        r = smtp_check(
            "exists@test.com", "test.com", [host],
            helo=HELO, timeout=5, port=port, retries=0, delay=0,
        )
        assert r.status == "valid"
        assert r.code == 250
        assert r.catch_all is False

    def test_invalid(self, truthful_server: tuple[str, int]) -> None:
        host, port = truthful_server
        r = smtp_check(
            "nobody@test.com", "test.com", [host],
            helo=HELO, timeout=5, port=port, retries=0, delay=0,
        )
        assert r.status == "invalid"
        assert r.code == 550

    def test_catchall_detected(self, catchall_server: tuple[str, int]) -> None:
        host, port = catchall_server
        r = smtp_check(
            "anyone@test.com", "test.com", [host],
            helo=HELO, timeout=5, port=port, retries=0, delay=0,
        )
        assert r.status == "valid"
        assert r.catch_all is True

    def test_greylist_with_retry(self, greylist_server: tuple[str, int]) -> None:
        host, port = greylist_server
        r = smtp_check(
            "user@test.com", "test.com", [host],
            helo=HELO, timeout=5, port=port, retries=1, delay=0.1,
        )
        assert r.status == "valid"
        assert r.greylist_retries == 1

    def test_mailbox_full(self, full_server: tuple[str, int]) -> None:
        host, port = full_server
        r = smtp_check(
            "user@test.com", "test.com", [host],
            helo=HELO, timeout=5, port=port, retries=0, delay=0,
        )
        assert r.status == "valid"
        assert r.code == 552

    def test_no_catchall_check(self, truthful_server: tuple[str, int]) -> None:
        host, port = truthful_server
        r = smtp_check(
            "exists@test.com", "test.com", [host],
            helo=HELO, timeout=5, port=port, retries=0, delay=0,
            detect_catch_all=False,
        )
        assert r.status == "valid"
        assert r.catch_all is None
