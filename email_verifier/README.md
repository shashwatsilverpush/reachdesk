# email_verifier

A 6-stage email verification pipeline that checks whether an email address
exists, going well beyond a single SMTP `RCPT TO` probe.

```
syntax → DNS → SMTP probe → catch-all detection → provider escalation → verdict
```

## What it can and cannot do

**Can do:**

- Validate email syntax (RFC 5321), normalize case, detect IDN domains.
- Resolve MX records, detect Null MX (RFC 7505), fall back to A/AAAA.
- Probe SMTP servers with `RCPT TO` and interpret response codes.
- Detect catch-all (wildcard) domains that accept any local part.
- Flag disposable, role-based, and free-provider addresses.
- Suggest typo corrections for popular domain misspellings.

**Cannot do (by design):**

- **Big providers hide mailbox existence.** Gmail, Outlook, Yahoo, and
  others accept `RCPT TO` for _any_ local part — a `250 OK` does not mean
  the mailbox exists. These addresses get verdict `unknown` with reason
  `hidden_provider`.
- **No signup-form scraping.** Automating provider signup pages violates
  their ToS, hits CAPTCHAs, and breaks constantly. A disabled hook is
  provided but never enabled by default.
- **No inbox placement testing.** This tool checks address existence, not
  deliverability reputation.

## Installation

```bash
# Clone and set up
cd email_verifier
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

## Usage

```bash
# Single address
python -m email_verifier verify user@example.com

# Multiple addresses
python -m email_verifier verify alice@acme.com bob@corp.net

# From a file (one per line, '#' comments allowed)
python -m email_verifier verify -f addresses.txt

# JSON output
python -m email_verifier verify user@example.com --json

# Append JSONL to a file
python -m email_verifier verify user@example.com --out results.jsonl

# Custom sender and timeout
python -m email_verifier verify user@example.com -s me@mydomain.com -t 30

# Skip catch-all detection
python -m email_verifier verify user@example.com --no-catch-all

# Override lists via TOML config
python -m email_verifier verify user@example.com --config overrides.toml
```

### CLI options

| Flag | Default | Description |
|------|---------|-------------|
| `-f, --file PATH` | | File with one address per line |
| `-s, --sender ADDR` | `verify@example.org` | MAIL FROM address |
| `--helo NAME` | local FQDN | EHLO hostname |
| `-t, --timeout SECS` | 15 | SMTP timeout |
| `--retries N` | 1 | Greylist retries |
| `--delay SECS` | 5 | Delay between probes/retries |
| `--no-catch-all` | | Skip catch-all detection |
| `--json` | | JSON array output |
| `--out PATH` | | Append JSONL to file |
| `--config PATH` | | TOML overrides for lists/profiles |
| `--enable-signup-probe` | | Exits with explanation (see Stage 5) |

## Output

### Table (default)

```
EMAIL                                    VERDICT   MX                               REASON                 FLAGS            SUGGESTION
----------------------------------------------------------------------------------------------------------------------------------
alice@acme-corp.com                      valid     mx1.acme-corp.com                smtp_confirmed         -                -
bad..address@gmail.com                   invalid   -                                syntax                 free             -
```

### JSON

```json
{
  "email": "alice@acme-corp.com",
  "verdict": "valid",
  "reason": "smtp_confirmed",
  "flags": [],
  "suggestion": null,
  "evidence": {
    "mx": ["mx1.acme-corp.com"],
    "rcpt_code": 250,
    "rcpt_message": "OK",
    "catch_all": false,
    "greylist_retries": 0,
    "notes": []
  }
}
```

## Testing with a mock server

The test suite includes mock SMTP servers built with `aiosmtpd`. To run
tests against the mock servers without any network access:

```bash
pytest -q
```

To verify a specific address against a local test server you set up
yourself:

```bash
# Start a test server on port 2525 (not included — use aiosmtpd CLI or similar)
python -m email_verifier verify user@example.com --port 2525
```

## Verdicts and reasons

| Verdict | Reason | Meaning |
|---------|--------|---------|
| `invalid` | `syntax` | Malformed address |
| `invalid` | `no_dns` | Domain has no DNS records |
| `invalid` | `no_mx` | Domain exists but has no MX or A/AAAA fallback |
| `invalid` | `smtp_rejected_550` | Server explicitly rejected the recipient |
| `valid` | `mailbox_full` | 552 — mailbox full but exists |
| `valid` | `smtp_confirmed` | Server accepted the recipient |
| `unknown` | `greylisted` | Temporary rejection after all retries |
| `unknown` | `unreachable` | Could not connect to any MX |
| `unknown` | `hidden_provider` | Catch-all + free provider (Gmail, etc.) |
| `risky` | `catch_all_domain` | Catch-all corporate domain |
| `risky` | `disposable` | Disposable email domain |

## Important considerations

### Port 25 blocking

Most cloud providers (AWS, GCP, Azure) block outbound port 25 by default.
You need to either:
- Request port 25 to be unblocked for your instance.
- Use a VPS provider that allows port 25.
- Route through a relay that has port 25 access.

### Sender domain matters

SMTP servers may reject probes from untrustworthy senders. For best
results:
- Use a real domain you control with valid SPF, DKIM, and PTR records.
- The EHLO hostname should resolve to your IP.
- The MAIL FROM domain should have proper DNS configuration.

### Greylisting

Many servers temporarily reject (4xx) the first connection from an unknown
sender and expect a retry after ~15 minutes. The default `--retries 1` with
`--delay 5` is a compromise — real greylisting windows are typically 5–15
minutes. Increase `--delay` for more thorough verification at the cost of
time.

### Provider behavior drift

The provider profiles in `providers.py` are a snapshot. Major providers
change their SMTP behavior without notice. Re-test periodically and use
`--config` to override profiles.

## Responsible use

- **Verify only addresses you have a legitimate reason to check.** Bulk
  verification of scraped lists is abuse.
- **Respect throttling defaults.** The tool enforces per-address delays
  (`--delay`, default 5s) and one RCPT per connection. Do not disable
  these safeguards.
- **Probing aggressively gets IPs blacklisted.** SMTP servers track
  probe patterns. Rapid-fire verification from a single IP will get you
  listed on DNSBLs (Spamhaus, Barracuda, etc.).
- **Signup automation violates provider ToS.** The `--enable-signup-probe`
  flag exists as a hook but ships disabled. Implementing it for Gmail,
  Yahoo, etc. violates their terms and will break.
- **This tool is for operational email hygiene**, not for harvesting or
  spam enablement.

## Configuration overrides (TOML)

Create an `overrides.toml` with any of these keys:

```toml
role_accounts = ["info", "admin", "custom-role"]
disposable_domains = ["mailinator.com", "my-disposable.example"]
popular_domains = ["gmail.com", "mycompany.com"]
free_providers = ["gmail.com", "yahoo.com"]

[provider_profiles.mycompany.com]
family = "custom"
smtp_reveals = true
greylist_prone = false
```

## Dependencies

- **Runtime:** `dnspython` (DNS resolution)
- **Dev:** `pytest`, `aiosmtpd` (mock SMTP servers for testing)
