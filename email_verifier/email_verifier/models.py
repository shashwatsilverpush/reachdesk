"""Data models for email verification results."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

Verdict = Literal["valid", "invalid", "unknown", "risky"]


@dataclass
class Evidence:
    """Evidence collected during the verification pipeline."""

    mx: list[str] = field(default_factory=list)
    rcpt_code: int | None = None
    rcpt_message: str | None = None
    catch_all: bool | None = None
    greylist_retries: int = 0
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        """Serialize to a plain dict for JSON output."""
        return {
            "mx": self.mx,
            "rcpt_code": self.rcpt_code,
            "rcpt_message": self.rcpt_message,
            "catch_all": self.catch_all,
            "greylist_retries": self.greylist_retries,
            "notes": self.notes,
        }


@dataclass
class Result:
    """Complete result of verifying a single email address."""

    email: str
    verdict: Verdict
    reason: str
    flags: list[str] = field(default_factory=list)
    suggestion: str | None = None
    evidence: Evidence = field(default_factory=Evidence)

    def to_dict(self) -> dict:
        """Serialize to a plain dict for JSON output."""
        return {
            "email": self.email,
            "verdict": self.verdict,
            "reason": self.reason,
            "flags": self.flags,
            "suggestion": self.suggestion,
            "evidence": self.evidence.to_dict(),
        }
