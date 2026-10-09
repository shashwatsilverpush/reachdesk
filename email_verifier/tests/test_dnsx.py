"""Tests for Stage 2 — DNS MX resolution."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import dns.resolver
import pytest

from email_verifier.dnsx import clear_mx_cache, get_mx_hosts


# ---------------------------------------------------------------------------
#  Mock resolver helpers
# ---------------------------------------------------------------------------

@dataclass
class MockMX:
    preference: int
    exchange: str

    def __str__(self) -> str:
        return self.exchange


class MockAnswer:
    def __init__(self, records: list[Any]) -> None:
        self._records = records

    def __iter__(self):
        return iter(self._records)


class FakeResolver:
    """Configurable mock DNS resolver."""

    def __init__(
        self,
        mx: list[MockMX] | None = None,
        a_exists: bool = False,
        nxdomain: bool = False,
        null_mx: bool = False,
    ) -> None:
        self._mx = mx
        self._a_exists = a_exists
        self._nxdomain = nxdomain
        self._null_mx = null_mx

    def resolve(self, name: str, rdtype: str, lifetime: float | None = None) -> MockAnswer:
        if self._nxdomain:
            raise dns.resolver.NXDOMAIN

        if rdtype == "MX":
            if self._null_mx:
                return MockAnswer([MockMX(0, ".")])
            if self._mx is not None:
                return MockAnswer(self._mx)
            raise dns.resolver.NoAnswer

        if rdtype in ("A", "AAAA"):
            if self._a_exists:
                return MockAnswer(["127.0.0.1"])
            raise dns.resolver.NoAnswer

        raise dns.resolver.NoAnswer


@pytest.fixture(autouse=True)
def _clear() -> None:
    clear_mx_cache()


# ---------------------------------------------------------------------------
#  Tests
# ---------------------------------------------------------------------------

class TestMXResolution:
    def test_normal_mx_sorted_by_preference(self) -> None:
        resolver = FakeResolver(
            mx=[MockMX(20, "mx2.example.com"), MockMX(10, "mx1.example.com")]
        )
        hosts = get_mx_hosts("example.com", resolver=resolver)
        assert hosts == ["mx1.example.com", "mx2.example.com"]

    def test_strips_trailing_dot(self) -> None:
        resolver = FakeResolver(mx=[MockMX(10, "mx.example.com.")])
        hosts = get_mx_hosts("example.com", resolver=resolver)
        assert hosts == ["mx.example.com"]


class TestNoMX:
    def test_no_mx_no_a_returns_empty(self) -> None:
        resolver = FakeResolver(mx=None, a_exists=False)
        hosts = get_mx_hosts("noemail.example.com", resolver=resolver)
        assert hosts == []

    def test_no_mx_with_a_falls_back(self) -> None:
        resolver = FakeResolver(mx=None, a_exists=True)
        hosts = get_mx_hosts("webonly.example.com", resolver=resolver)
        assert hosts == ["webonly.example.com"]


class TestNullMX:
    def test_null_mx_returns_empty(self) -> None:
        resolver = FakeResolver(null_mx=True)
        hosts = get_mx_hosts("nomx.example.com", resolver=resolver)
        assert hosts == []


class TestNXDOMAIN:
    def test_nxdomain_raises(self) -> None:
        resolver = FakeResolver(nxdomain=True)
        with pytest.raises(dns.resolver.NXDOMAIN):
            get_mx_hosts("nonexistent.example.com", resolver=resolver)
