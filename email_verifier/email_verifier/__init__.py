"""Email verification pipeline."""

from email_verifier.models import Evidence, Result
from email_verifier.pipeline import verify_email

__all__ = ["verify_email", "Result", "Evidence"]
