"""Tests for Stage 6 — full pipeline and verdict mapping."""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass
from typing import Any

import dns.resolver
import pytest

from email_verifier.models import Result
from email_verifier.pipeline import verify_email

HELO = "test.local"


# ---------------------------------------------------------------------------
#  Mock DNS resolver that points to local mock SMTP servers
# ---------------------------------------------------------------------------

@dataclass
class MockMX:
    preference: int
    exchange: str


class MockAnswer:
    def __init__(self, records: list[Any]) -> None:
        self._records = records

    def __iter__(self):
        return iter(self._records)


class LocalResolver:
    """Resolver that returns the mock server's hostname as the sole MX."""

    def __init__(self, mx_host: str, a_exists: bool = True) -> None:
        self._mx_host = mx_host
        self._a_exists = a_exists

    def resolve(self, name: str, rdtype: str, lifetime: float | None = None) -> MockAnswer:
        if rdtype == "MX":
            return MockAnswer([MockMX(10, self._mx_host)])
        if rdtype in ("A", "AAAA"):
            if self._a_exists:
                return MockAnswer(["127.0.0.1"])
            raise dns.resolver.NoAnswer
        raise dns.resolver.NoAnswer


class NXDOMAINResolver:
    """Resolver that always raises NXDOMAIN."""

    def resolve(self, name: str, rdtype: str, lifetime: float | None = None) -> None:
        raise dns.resolver.NXDOMAIN


class NoMXResolver:
    """Resolver with no MX and no A records."""

    def resolve(self, name: str, rdtype: str, lifetime: float | None = None) -> None:
        raise dns.resolver.NoAnswer


class NullMXResolver:
    """Resolver that returns a Null MX record (RFC 7505)."""

    def resolve(self, name: str, rdtype: str, lifetime: float | None = None) -> MockAnswer:
        if rdtype == "MX":
            return MockAnswer([MockMX(0, ".")])
        raise dns.resolver.NoAnswer


# ---------------------------------------------------------------------------
#  Pipeline tests
# ---------------------------------------------------------------------------

class TestSyntaxStage:
    def test_invalid_syntax(self) -> None:
        r = verify_email("bad..address@gmail.com")
        assert r.verdict == "invalid"
        assert r.reason == "syntax"

    def test_double_dot_in_local(self) -> None:
        r = verify_email("user..name@example.com")
        assert r.verdict == "invalid"
        assert r.reason == "syntax"


class TestDNSStage:
    def test_no_dns(self) -> None:
        r = verify_email(
            "user@nonexistent.example.com",
            dns_resolver=NXDOMAINResolver(),
        )
        assert r.verdict == "invalid"
        assert r.reason == "no_dns"

    def test_no_mx(self) -> None:
        r = verify_email(
            "user@noemail.example.com",
            dns_resolver=NoMXResolver(),
        )
        assert r.verdict == "invalid"
        assert r.reason in ("no_mx", "no_dns")

    def test_null_mx(self) -> None:
        r = verify_email(
            "user@nullmx.example.com",
            dns_resolver=NullMXResolver(),
        )
        assert r.verdict == "invalid"
        assert r.reason in ("no_mx", "no_dns")


class TestSMTPVerdicts:
    def test_valid_smtp_confirmed(self, truthful_server: tuple[str, int]) -> None:
        host, port = truthful_server
        r = verify_email(
            "exists@corporate.example.com",
            helo=HELO, port=port,
            dns_resolver=LocalResolver(host),
            retries=0, delay=0,
        )
        assert r.verdict == "valid"
        assert r.reason == "smtp_confirmed"
        assert r.evidence.catch_all is False

    def test_invalid_smtp_rejected(self, truthful_server: tuple[str, int]) -> None:
        host, port = truthful_server
        r = verify_email(
            "nobody@corporate.example.com",
            helo=HELO, port=port,
            dns_resolver=LocalResolver(host),
            retries=0, delay=0,
        )
        assert r.verdict == "invalid"
        assert r.reason == "smtp_rejected_550"

    def test_mailbox_full(self, full_server: tuple[str, int]) -> None:
        host, port = full_server
        r = verify_email(
            "user@corporate.example.com",
            helo=HELO, port=port,
            dns_resolver=LocalResolver(host),
            retries=0, delay=0,
        )
        assert r.verdict == "valid"
        assert r.reason == "mailbox_full"
        assert r.evidence.rcpt_code == 552


class TestCatchAllVerdicts:
    def test_catchall_free_provider_hidden(self, catchall_server: tuple[str, int]) -> None:
        """Catch-all + free provider -> unknown / hidden_provider."""
        host, port = catchall_server
        r = verify_email(
            "user@gmail.com",
            helo=HELO, port=port,
            dns_resolver=LocalResolver(host),
            retries=0, delay=0,
        )
        assert r.verdict == "unknown"
        assert r.reason == "hidden_provider"

    def test_catchall_corporate_risky(self, catchall_server: tuple[str, int]) -> None:
        """Catch-all + corporate domain -> risky / catch_all_domain."""
        host, port = catchall_server
        r = verify_email(
            "user@bigcorp.example.com",
            helo=HELO, port=port,
            dns_resolver=LocalResolver(host),
            retries=0, delay=0,
        )
        assert r.verdict == "risky"
        assert r.reason == "catch_all_domain"
        assert r.evidence.catch_all is True


class TestFlagVerdicts:
    def test_disposable_caps_at_risky(self, truthful_server: tuple[str, int]) -> None:
        """Disposable domain caps verdict at risky."""
        host, port = truthful_server
        r = verify_email(
            "exists@mailinator.com",
            helo=HELO, port=port,
            dns_resolver=LocalResolver(host),
            retries=0, delay=0,
        )
        assert r.verdict == "risky"
        assert r.reason == "disposable"
        assert "disposable" in r.flags

    def test_role_account_flagged(self) -> None:
        """Role account flag is set from syntax check."""
        r = verify_email(
            "admin@nonexistent-domain.example.com",
            dns_resolver=NXDOMAINResolver(),
        )
        assert "role" in r.flags

    def test_disposable_domain_flagged(self) -> None:
        r = verify_email(
            "user@mailinator.com",
            dns_resolver=NXDOMAINResolver(),
        )
        assert "disposable" in r.flags


class TestGreylist:
    def test_greylist_retries(self, greylist_server: tuple[str, int]) -> None:
        host, port = greylist_server
        r = verify_email(
            "user@corporate.example.com",
            helo=HELO, port=port,
            dns_resolver=LocalResolver(host),
            retries=1, delay=0.1,
        )
        assert r.verdict == "valid"
        assert r.evidence.greylist_retries == 1


class TestAppleLikeBehavior:
    def test_apple_like_catch_all_probe_failed(self, apple_like: tuple[str, int]) -> None:
        host, port = apple_like
        r = verify_email(
            "exists@icloud-test.example.com",
            helo=HELO, port=port,
            dns_resolver=LocalResolver(host),
            retries=0, delay=0,
        )
        assert r.verdict == "valid"
        assert r.reason == "smtp_confirmed"
        assert "catch_all_probe_failed" in r.evidence.notes


class TestJSONOutput:
    def test_to_dict_schema(self, truthful_server: tuple[str, int]) -> None:
        host, port = truthful_server
        r = verify_email(
            "exists@corporate.example.com",
            helo=HELO, port=port,
            dns_resolver=LocalResolver(host),
            retries=0, delay=0,
        )
        d = r.to_dict()
        assert "email" in d
        assert "verdict" in d
        assert "reason" in d
        assert "flags" in d
        assert "suggestion" in d
        assert "evidence" in d
        assert "mx" in d["evidence"]
        assert "rcpt_code" in d["evidence"]
        assert "catch_all" in d["evidence"]


class TestCLI:
    def test_json_output_syntax_error(self) -> None:
        """CLI --json for a syntax-invalid address produces parseable JSON."""
        result = subprocess.run(
            [sys.executable, "-m", "email_verifier", "verify",
             "bad..address@gmail.com", "--json", "--port", "0"],
            capture_output=True, text=True,
            cwd=str(__import__("pathlib").Path(__file__).resolve().parent.parent),
        )
        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert isinstance(data, list)
        assert len(data) == 1
        assert data[0]["verdict"] == "invalid"
        assert data[0]["reason"] == "syntax"

    def test_jsonl_output(self, tmp_path) -> None:
        """CLI --out appends JSONL."""
        out_file = tmp_path / "results.jsonl"
        result = subprocess.run(
            [sys.executable, "-m", "email_verifier", "verify",
             "bad..address@gmail.com", "--out", str(out_file), "--port", "0"],
            capture_output=True, text=True,
            cwd=str(__import__("pathlib").Path(__file__).resolve().parent.parent),
        )
        assert result.returncode == 0
        lines = out_file.read_text().strip().split("\n")
        assert len(lines) == 1
        obj = json.loads(lines[0])
        assert obj["verdict"] == "invalid"
