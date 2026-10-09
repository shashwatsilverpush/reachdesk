# Leadcore

**Corporate email pattern inference engine with real-time verification**

Python 3.14 · FastAPI · SQLAlchemy · Oct 2026 · 10 commits

---

## What Leadcore Does

Leadcore ingests observed corporate email addresses from multiple sources, infers per-domain email formatting patterns (e.g. `first.last`, `flast`), and generates candidate addresses for new contacts. A local web UI lets you look up people via LinkedIn/Apollo, run pattern searches, and verify results via SMTP, M365 probes, and external APIs.

| Metric | Count |
|--------|-------|
| Lines of Python | 7,775 |
| Lines of tests | 1,798 |
| Source files | 65 |
| Web UI tabs | 5 |
| Unit tests | 87 |
| Integration tests | 17 |

---

## Architecture

```
Data Sources → Ingest & Normalize → Pattern Inference → Candidate Generation → Verification
```

### Two packages, one tool

- **`leadcore`** — the core engine: data ingestion, name normalization, pattern inference, tiered grading, candidate generation, collision detection, and the web UI.
- **`email_verifier`** — standalone verification tooling: DNS/MX resolution, SMTP probing, Microsoft 365 account probing, and external API verification (Hunter.io, ZeroBounce).

---

## Tiering System

Every domain is assigned a confidence tier based on how many distinct email observations exist and how consistent they are:

| Tier | Criteria | Confidence |
|------|----------|------------|
| **A** | 3+ samples, dominant pattern, recent data | High |
| **B** | 2 agreeing samples, or a resolved temporal split | Good |
| **C+** | 1 sample using full first+last format | Moderate |
| **C-** | 1 sample using a shorter format | Low-moderate |
| **D** | Conflicting patterns across samples | Conflicted |
| **E** | No usable samples | None |

---

## Data Sources

| Source | Type | Weight |
|--------|------|--------|
| LinkedIn confirmed | Direct observation | Highest |
| Apollo verified addresses | Provider-sourced | High (trusts provider surnames) |
| Own correspondence | Sent/received emails | Medium-high |
| Lead_list (own DB export) | Internal database | Medium (less curated) |
| Unknown provenance | Other | Lowest |

Sample weight = recency decay × source confidence × delivery confirmation bonus.

---

## Verification Stack

A multi-layer approach resolves email existence with increasing specificity:

### Layer 1: SMTP Probing

Direct SMTP `RCPT TO` probes against the domain's MX servers. Works well for domains that reject unknown addresses, but unreliable on catch-all domains (Google Workspace, some Exchange setups).

**Unreliability detection:** When SMTP returns some "found" but zero rejections, results are marked unreliable and overridden by external verification.

### Layer 2: Microsoft 365 Probe

Uses Microsoft's unauthenticated `GetCredentialType` endpoint to check if an email maps to an Azure AD account. Resolves catch-all ambiguity for M365-hosted domains. Scans all 15 common patterns in one pass.

### Layer 3: External API Verification

Hunter.io (primary) and ZeroBounce (fallback) resolve catch-all domains that SMTP and M365 can't. Credit-aware: checks available balance before starting, verifies high-specificity patterns first, stops early on definitive matches.

- **Hunter:** 100 credits/month (refreshes)
- **ZeroBounce:** 100 one-time credits

---

## Pattern Specificity & Confidence

Each email pattern is scored 1–3 based on how uniquely it identifies a person:

| Score | Level | Patterns | Confidence |
|-------|-------|----------|------------|
| **3** | Full name | `first.last` `firstlast` `first_last` `first-last` `last.first` `lastfirst` `last_first` | High |
| **2** | Partial name | `flast` `f.last` `firstl` `first.l` `lastf` | Medium |
| **1** | Generic | `first` `last` `fl` | Low |

This scoring prevents false positives: a match on `kumar@` (specificity 1) won't be chosen over `sandeep.kumar@` (specificity 3).

---

## Web UI Tabs

### Dashboard
Overview of the database: observation counts, inference run history, tier distribution.

### Firms
Browse all tracked domains with their inferred patterns, tier grades, and sample counts. Click through to firm detail for observations and pattern history.

### Generate
The main workflow tab. Paste a LinkedIn profile URL → auto-lookup via Apollo (with slug-based name recovery and staleness warnings) → generate candidate addresses → run pattern search for unconfirmed domains → see confidence-scored results with evidence.

- **LinkedIn/Apollo lookup** — auto-fills name and domain; recovers full last name from slug when Apollo returns only an initial; warns about stale company data
- **Pattern search** — scans 15 common patterns against M365 or SMTP; detects unreliable SMTP results; falls back to external API verification with smart credit spending
- **Evidence display** — color-coded confidence badges, specificity-scored pattern pills, reasoning text explaining why a particular address was chosen
- **Credit tracking** — shows verification credits used and remaining balances

### Verification
Import external verification results (xlsx), export candidates for checking, and run live SMTP verification through the built-in pipeline.

### Validation
Accuracy scorecard comparing our predicted addresses against externally revealed ones, broken down by tier. Predictions computed in Python (no Excel formula dependency).

---

## Key Files

| File | Lines | Role |
|------|-------|------|
| `src/leadcore/web/app.py` | 567 | Web UI — all routes, LinkedIn lookup, pattern search orchestration |
| `src/leadcore/patterns/inference.py` | 291 | Pattern aggregation, weighting, tiering logic |
| `src/leadcore/models.py` | 287 | SQLAlchemy models — observations, contacts, domain patterns |
| `email_verifier/.../pipeline.py` | 255 | SMTP verification pipeline orchestration |
| `email_verifier/.../smtp_probe.py` | 193 | Direct SMTP RCPT TO probing |
| `email_verifier/.../ext_verify.py` | 177 | Hunter.io & ZeroBounce API integration, credit-aware verification |
| `src/leadcore/generate/candidates.py` | 170 | Candidate address generation with collision detection |
| `email_verifier/.../ms_probe.py` | 141 | M365 GetCredentialType probe, pattern search |
| `src/leadcore/patterns/library.py` | 103 | 15 named pattern templates with render() |

---

## Session Work (Oct 2026)

Changes implemented across 5 files (+221 lines):

### SMTP False Positive Detection
Domains that return inconsistent SMTP results (some "found", zero rejections) are detected as unreliable. Their "found" results are downgraded to "unknown" and external verification is triggered to provide ground truth. Fixed the `wppmedia.com` case where generic patterns (`kumar@`, `sk@`) were falsely shown as found.

### Smart Credit-Aware Verification
New `ext_verify_patterns()` function checks available API credits before starting, sorts patterns by specificity (full-name first), and stops early once a definitive match is found. Reduced the `wppmedia.com` search from 15 credits to 7.

### Pattern Specificity & Confidence Scoring
Each of the 15 patterns assigned a specificity score (1–3). The best confirmed pattern is selected by specificity, not by first-found. Confidence levels (High/Medium/Low) with reasoning text are displayed to the user.

### LinkedIn Slug Name Recovery
When Apollo returns a single-character last name (e.g. "T" for Dennis Tsauder), the system cross-references the LinkedIn URL slug to extract the full last name. Warnings are surfaced when Apollo data appears stale or names mismatch.

### Validation Tab Fixes
Excel formula dependencies replaced with Python-side computation using `Pattern.render()`. Fixed column overflow with `table-layout:fixed` and hover-to-expand for truncated cells.

---

## Validation Results

Apollo A/B test from the validation sheet:

| Metric | Accuracy |
|--------|----------|
| A & B tier | 86% |
| C tier overall | 69% |
| C+ (full-name) | 94% |
| C- (short form) | 44% |

Validated firms are folded back into the training data for future inference runs.

---

## Running the Tool

- **Web UI:** `leadcore web` → `http://127.0.0.1:8000`
- **CLI pipeline:** `leadcore ingest` → `leadcore infer` → `leadcore generate`
- **Tests:** `pytest tests/` (87 unit) · `pytest tests/test_integration.py` (17 integration)
- **Email verifier tests:** `cd email_verifier && pytest` (13 unit)

> API keys are configured exclusively via `.env` — never exposed in the frontend.
