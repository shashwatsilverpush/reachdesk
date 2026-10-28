"""Command-line interface for the email verification pipeline."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

from email_verifier.dnsx import clear_mx_cache
from email_verifier.lists import load_overrides
from email_verifier.models import Result
from email_verifier.pipeline import verify_email
from email_verifier.providers import DisabledSignupProbe


def _build_parser() -> argparse.ArgumentParser:
    """Build the argument parser."""
    p = argparse.ArgumentParser(
        prog="email_verifier",
        description=(
            "Check whether email addresses exist via a 6-stage verification "
            "pipeline (syntax → DNS → SMTP probe → catch-all → provider → verdict).\n\n"
            "⚠  Responsible use: verify only addresses you have a legitimate reason "
            "to check.  Aggressive probing gets IPs blacklisted."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = p.add_subparsers(dest="command")

    verify = sub.add_parser("verify", help="Verify one or more email addresses")
    verify.add_argument("emails", nargs="*", help="addresses to check")
    verify.add_argument(
        "-f", "--file", help="file with one address per line ('#' comments allowed)"
    )
    verify.add_argument(
        "-s", "--sender", default="",
        help="MAIL FROM address (default: verify@<target-domain>)",
    )
    verify.add_argument(
        "--helo", help="hostname for the EHLO command (default: local FQDN)"
    )
    verify.add_argument(
        "-t", "--timeout", type=int, default=15, help="SMTP timeout in seconds (default: 15)"
    )
    verify.add_argument(
        "--retries", type=int, default=1, help="greylist retries (default: 1)"
    )
    verify.add_argument(
        "--delay", type=float, default=5.0,
        help="delay in seconds between probes/retries (default: 5)",
    )
    verify.add_argument(
        "--no-catch-all", action="store_true", help="skip the catch-all detection probe"
    )
    verify.add_argument(
        "--json", action="store_true", dest="json_output",
        help="print JSON array instead of the table",
    )
    verify.add_argument(
        "--out", metavar="PATH",
        help="append one JSON object per line (JSONL) to PATH",
    )
    verify.add_argument(
        "--config", metavar="PATH",
        help="TOML overrides for lists/profiles",
    )
    verify.add_argument(
        "--enable-signup-probe", action="store_true",
        help="enable signup-probe hooks (see docs)",
    )
    verify.add_argument(
        "--port", type=int, default=25,
        help=argparse.SUPPRESS,  # for testing only
    )

    return p


def main(argv: list[str] | None = None) -> None:
    """CLI entry point."""
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command != "verify":
        parser.print_help()
        sys.exit(1)

    if args.enable_signup_probe:
        sys.exit(
            "Error: --enable-signup-probe requires a concrete SignupProbe class "
            "supplied via --config.  No built-in signup probe is shipped because:\n"
            "  • Automating provider signup forms violates their Terms of Service.\n"
            "  • Providers deploy CAPTCHAs that break automated probes quickly.\n"
            "  • HTTP scraping is fragile and provider-specific.\n"
            "Provide your own implementation in the config file."
        )

    emails: list[str] = list(args.emails)
    if args.file:
        with open(args.file, encoding="utf-8") as fh:
            emails += [
                ln.strip()
                for ln in fh
                if ln.strip() and not ln.lstrip().startswith("#")
            ]

    if not emails:
        parser.error("no addresses given (pass them as arguments or use --file)")

    overrides: dict[str, Any] | None = None
    if args.config:
        overrides = load_overrides(args.config)

    clear_mx_cache()

    results: list[Result] = []
    for i, addr in enumerate(emails):
        r = verify_email(
            addr,
            sender=args.sender,
            helo=args.helo,
            timeout=args.timeout,
            port=args.port,
            retries=args.retries,
            delay=args.delay,
            detect_catch_all=not args.no_catch_all,
            overrides=overrides,
            signup_probe=DisabledSignupProbe(),
        )
        results.append(r)
        if i < len(emails) - 1 and args.delay:
            time.sleep(args.delay)

    if args.json_output:
        print(json.dumps([r.to_dict() for r in results], indent=2))
    else:
        _print_table(results)

    if args.out:
        with open(args.out, "a", encoding="utf-8") as fh:
            for r in results:
                fh.write(json.dumps(r.to_dict()) + "\n")


def _print_table(results: list[Result]) -> None:
    """Print results as a formatted table."""
    print(
        f"{'EMAIL':<40} {'VERDICT':<9} {'MX':<32} {'REASON':<22} {'FLAGS':<16} SUGGESTION"
    )
    print("-" * 130)
    for r in results:
        mx = r.evidence.mx[0] if r.evidence.mx else "-"
        flags_str = ",".join(r.flags) if r.flags else "-"
        suggestion = r.suggestion or "-"
        print(
            f"{r.email:<40} {r.verdict:<9} {mx:<32} {r.reason:<22} {flags_str:<16} {suggestion}"
        )
