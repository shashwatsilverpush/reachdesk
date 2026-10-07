# leadcore

Contact data foundation and deterministic email-pattern engine (Workstreams C and D).
It ingests our contact lists, learns each company domain's address convention, and
generates candidate addresses for named people at those domains. No sending, no mailbox
access, no LLM calls.

## Setup

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -e '.[dev]'
```

The source files go in `data/raw/` as `Reachouts_.xlsx` and `Lead_list.xlsx`.
The database defaults to `./leadcore.db` (SQLite). Override it with `--db URL` or `LEADCORE_DB_URL`.
Every command runs `alembic upgrade head` first.

```bash
leadcore ingest [--source all|reachouts|leadlist]
leadcore infer [--rebuild]          # baseline pass + nickname pass + delta report
leadcore generate --domain verve.com --first Robert --last Smith [--dry-run]
leadcore report patterns            # data/out/patterns.xlsx
leadcore report capture-sheet [--per-tier 24] [--seed 7]
leadcore stats                      # counts + acceptance comparison
pytest                              # LEADCORE_INTEGRATION=1 also runs the real-file test
```

## Layout

| Path | Role |
|---|---|
| `normalize/` | names (unicode, transliteration, surname/given variants, LinkedIn slugs), nicknames, emails |
| `patterns/library.py` | 25 patterns, each defined once as a token template; the generator and the matcher regex both derive from it |
| `patterns/matcher.py` | residual-surname inference; `matches()` returns the best explanation(s) |
| `patterns/inference.py` | per-domain samples, weighting, tiering, temporal split (pure functions) |
| `pipeline.py` | ingest → observations → confirmed contacts; versioned inference runs |
| `generate/` | candidates + cross/self collision checks |
| `report/` | pattern workbook and validation capture sheet |
| `models.py` | SQLAlchemy models; append-only guard; `Contact.sendable()` |

## Guarantees and how they're enforced

- **Append-only observations.** A `before_flush` hook rejects ORM updates and deletes of
  `EmailObservation`. A `do_orm_execute` hook rejects bulk `UPDATE`/`DELETE` against the table.
- **Idempotent ingest.** `obs_key = sha256(email, source_file, row_index)` is unique per tenant.
  Reachouts `source_file` includes the sheet (`Reachouts_.xlsx#2025`).
- **Reproducible inference.** Each run is immutable and fingerprinted on observation keys,
  tunables and a hash of the engine source (normalisers, matcher, exclusions, nickname CSV).
  If nothing changed, `infer` reuses the existing run; any code or config change produces a new one.
- **Provenance.** `Contact.sendable()` returns only `provenance_confirmed` contacts that are either
  `confirmed`, or `generated` with `canary_status = passed`. A `before_insert` hook rejects generated
  contacts without `derived_from_pattern_id`, and confirmed contacts carrying generation fields.
- **Portability.** Only generic types are used: non-native enums with CHECK constraints, and JSON.
  Postgres DDL compiles. Every table has a nullable `tenant_id`.

## Decisions to review

1. **Provenance by source.** Both files come from our own systems, so both are
   `provenance_confirmed = True` (`config.SOURCE_PROVENANCE_CONFIRMED`). They differ in sample weight:
   `Reachouts_` is the sequenced outreach log (`own_correspondence`, 0.8). `Lead_list` is an export of
   our own, less curated contact database (`own_database`, 0.65). A future file of unknown origin
   would be set to `False`. Its contacts would then be excluded from `sendable()` but would still
   seed inference, at weight 0.5.
2. **Dates.** There are no row-level dates. Reachouts rows are dated to 1 January of the sheet
   year. Lead_list rows use an assumed date (`config.LEADLIST_ASSUMED_DATE`, precision `assumed`),
   not the file mtime, which would change on every re-save. Assumed-date samples can't anchor a temporal split.
3. **Placeholder-named addresses are contacts.** `alex.lee@epochtimes.com` with name `there` is a real
   business contact, so it's stored and counted, but it can't test a pattern. This is the only definition
   that reproduces the 13,181 contact figure (+1.9%). Excluding those contacts gives 11,076.
4. **Unexplained samples count as dissent** (`config.UNEXPLAINED_IS_DISSENT`). An address with a real
   first name that no pattern explains blocks a "consistent" verdict. This is conservative, and it's the
   only reading under which "Tier D domains rescued by nicknames" is a meaningful metric.
5. **"Dominant" = ≥75% of sample weight** (`config.DOMINANCE_SHARE`). See the acceptance diagnosis below.
6. **Nickname readings rescue, they don't override.** A nickname reading is used only when the plain
   reading explains nothing, or explains only the first initial (`nate` for Nathan was `flast` with
   surname "ate"). Without this rule, `williams` for Will became `firstl` via "William".
7. **`firstlastinitial` is an alias of `firstl`.** They render identically, and two identical patterns
   would make every such sample permanently ambiguous.

## Acceptance diagnosis

Measured on the files in `data/raw/` on 2026-10-07, before the nickname pass:

| Metric | Expected | Measured | Δ | ±2% |
|---|---|---|---|---|
| Unique business contacts | 13,181 | 13,437 | +1.9% | yes |
| Unique company domains | 4,848 | 4,980 | +2.7% | no |
| Email overlap (business contacts) | 1,715 | 1,733 | +1.0% | yes |
| Rows resolving to exactly one pattern | ~93% | 95.7% | +2.7pp | no |
| Tier A | 827 | 1,138 | +37.6% | no |
| Tier B | 615 | 571 | −7.2% | no |
| Tier C | 1,950 | 1,615 | −17.2% | no |
| Tier D | 1,270 | 745 | −41.3% | no |
| Generatable A/B/C | 3,392 | 3,324 | −2.0% | borderline |

Diagnosis:

- **Input drift (corpus counts).** `Lead_list.xlsx` was last modified 2026-10-07 11:44, after the
  prior analysis. Contacts, domains and overlap all run 1–3% high in the same direction, which fits a
  slightly larger file. The overlap figure matches on business contacts (1,733). Overlap on all valid
  addresses is 1,778. Using only the spec's literal role/placeholder lists moves every count further
  away (contacts 13,619, domains 5,060), so the extended lists aren't the cause.
- **Tier E floor (C and D too low).** 821 domains have no contact with a usable first name: every
  address there was recorded as `there`, `team`, blank and so on. The constraint "placeholder names
  don't feed inference" forces these into Tier E. The prior figures imply only about 186 Tier E domains,
  which can't happen under that constraint. The prior analysis must have derived something from
  placeholder rows, for example structural reads of `x.y` locals. That would add domains to C and D,
  which matches where we're short.
- **A too high, D too low.** These track the dominance rule. With unanimity (`DOMINANCE_SHARE = 1.0`):
  A = 797 (−3.6%), B = 573, D = 1,084. So the prior "one dominant pattern" most likely meant *all
  samples agree*. I have not changed the threshold to match. It's a one-line config change if you
  confirm that was the intended rule.
- **Exactly-one resolution is higher (95.7%).** The matcher resolves ties by specificity (the reading
  that explains more characters with known name material wins). So `rob` for Rob is `first`, not also
  `flast` with surname "ob". The prior run probably counted those as ambiguous.

Nickname pass delta (current engine): **24 Tier D domains rescued into A/B/C**
(D→A 14, D→B 10). Also E→C 5, A→D 2. Unexplained samples fell from 404 to 362.
Final totals with nicknames: A 1,150 · B 581 · C 1,620 · D 723 · E 906 · generatable 3,351.
The two A→D moves are real conflicts the nickname table exposed. For example, `max@` and `nick@` at
cpmstar.com had been mis-read as `flast` with surnames "ax" and "ick".

## Known limitations

- Temporal splits rest on year-granularity dates (14 domains). Several have a single sample on the
  "new" side, so treat `migrated_from` as weak evidence until real send dates exist.
- The vendored nickname table is noisy (`robert → bill`). Generation caps variants at 4, ordered by
  first-name frequency in our corpus.
- Conventions outside the library come out unexplained or mis-labelled. One example is a Vietnamese
  given name plus family/middle initials: `viettq@` reads as `firstlast` with surname "tq".
- Collision checks compare first names only (nickname-aware), because most observations have no surname.
