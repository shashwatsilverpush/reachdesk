"""leadcore command line."""

from __future__ import annotations

from enum import Enum

import typer
from sqlalchemy import func, select

from leadcore import config, db, pipeline
from leadcore.models import Contact, DomainPattern, EmailObservation, Origin

app = typer.Typer(no_args_is_help=True, add_completion=False, help="Contact data foundation and pattern engine.")
report_app = typer.Typer(no_args_is_help=True, help="Write report artefacts to data/out/.")
app.add_typer(report_app, name="report")

STATE: dict[str, str | None] = {"db": None}

# Figures from the prior validated analysis (pre-nickname pass); see README.
ACCEPTANCE = {
    "contacts": 13181, "domains": 4848, "overlap": 1715, "exactly_one": 0.93,
    "A": 827, "B": 615, "C": 1950, "D": 1270, "ABC": 3392,
}


class Source(str, Enum):
    all = "all"
    reachouts = "reachouts"
    leadlist = "leadlist"
    apollo = "apollo"


@app.callback()
def main(db_url: str = typer.Option(None, "--db", help="Database URL (default: LEADCORE_DB_URL or ./leadcore.db)")):
    STATE["db"] = db_url
    db.migrate(db_url)


@app.command()
def ingest(source: Source = typer.Option(Source.all, "--source")):
    """Append observations from the raw files (idempotent) and refresh confirmed contacts."""
    sources = None if source == Source.all else [source.value]
    with db.session(STATE["db"]) as s:
        st = pipeline.ingest(s, sources)
    typer.echo(f"loaded {st.loaded} records: {st.inserted} new observations, "
               f"{st.already_present} already present")
    typer.echo(f"contacts: {st.contacts_created} created, {st.contacts_updated} updated")


def _tiers_line(c, grades=None) -> str:
    line = "  ".join(f"{t}={c.get(t, 0)}" for t in "ABCDE") + f"  A/B/C={sum(c.get(t, 0) for t in 'ABC')}"
    if grades:
        line += f"  (C+={grades.get('C+', 0)} C-={grades.get('C-', 0)})"
    return line


@app.command()
def infer(rebuild: bool = typer.Option(False, "--rebuild", help="Write a new run even if inputs are unchanged.")):
    """Infer per-domain patterns: a baseline pass, then the nickname pass, with a delta report."""
    with db.session(STATE["db"]) as s:
        base_run, base_new, base = pipeline.run_inference(s, use_nicknames=False, rebuild=rebuild)
        nick_run, nick_new, nick = pipeline.run_inference(s, use_nicknames=True, rebuild=rebuild)
        bc, nc = pipeline.tier_counts(s, base_run), pipeline.tier_counts(s, nick_run)
        ng = pipeline.grade_counts(s, nick_run)
    typer.echo(f"baseline run #{base_run.id}{'' if base_new else ' (unchanged, reused)'}:  {_tiers_line(bc)}")
    typer.echo(f"nickname run #{nick_run.id}{'' if nick_new else ' (unchanged, reused)'}:  {_tiers_line(nc, ng)}")
    _delta(base, nick)


def _delta(base: dict, nick: dict) -> None:
    from collections import Counter

    moves = Counter((base[d].tier, nick[d].tier) for d in base if base[d].tier != nick[d].tier)
    rescued = sum(n for (a, b), n in moves.items() if a == "D" and b in "ABC")
    typer.echo("\nNickname pass delta")
    typer.echo(f"  Tier D domains rescued into A/B/C: {rescued}")
    for (a, b), n in sorted(moves.items()):
        typer.echo(f"  {a} -> {b}: {n}")
    unexpl = sum(r.unexplained_count for r in base.values()), sum(r.unexplained_count for r in nick.values())
    typer.echo(f"  unexplained samples: {unexpl[0]} -> {unexpl[1]}")


@app.command()
def generate(domain: str = typer.Option(..., "--domain"), first: str = typer.Option(..., "--first"),
             last: str = typer.Option("", "--last"),
             dry_run: bool = typer.Option(False, "--dry-run", help="Show candidates without storing them.")):
    """Generate candidate addresses for one named person at a domain."""
    from leadcore.generate.candidates import Target, generate as gen

    with db.session(STATE["db"]) as s:
        run = pipeline.current_run(s, use_nicknames=True)
        if run is None:
            raise typer.BadParameter("no inference run yet: run `leadcore infer` first")
        dp = s.scalars(select(DomainPattern).where(DomainPattern.run_id == run.id,
                                                   DomainPattern.domain == domain.lower())).first()
        if dp:
            typer.echo(f"{dp.domain}: tier {dp.grade}, pattern {dp.pattern}, {dp.sample_count} samples")
        [res] = gen(s, [Target(first, last, domain)], run.id, persist=not dry_run)
    typer.echo(f"status: {res.status}" + (f" ({res.reason})" if res.reason else ""))
    for c in res.candidates:
        typer.echo(f"  {c.address:45} {c.kind}")
    for col in res.collisions:
        typer.echo(f"  collision[{col.kind}] {col.address} observed for {col.observed_name}")


@report_app.command("patterns")
def report_patterns():
    """Write the pattern workbook."""
    from leadcore.report import workbook

    with db.session(STATE["db"]) as s:
        run = pipeline.current_run(s, use_nicknames=True) or pipeline.current_run(s)
        if run is None:
            raise typer.BadParameter("no inference run yet: run `leadcore infer` first")
        typer.echo(f"wrote {workbook.build(s, run)}")


@report_app.command("tracker")
def report_tracker():
    """Write the firm tracker: every firm, issues first, with columns for the answers."""
    from leadcore.report import tracker

    with db.session(STATE["db"]) as s:
        run = pipeline.current_run(s, use_nicknames=True) or pipeline.current_run(s)
        if run is None:
            raise typer.BadParameter("no inference run yet: run `leadcore infer` first")
        typer.echo(f"wrote {tracker.build(s, run)}")


@report_app.command("capture-sheet")
def report_capture(per_tier: int = typer.Option(config.CAPTURE_SHEET_PER_TIER, "--per-tier"),
                   seed: int = typer.Option(config.CAPTURE_SHEET_SEED, "--seed")):
    """Write the stratified validation capture sheet."""
    from leadcore.report import capture_sheet

    with db.session(STATE["db"]) as s:
        run = pipeline.current_run(s, use_nicknames=True) or pipeline.current_run(s)
        if run is None:
            raise typer.BadParameter("no inference run yet: run `leadcore infer` first")
        typer.echo(f"wrote {capture_sheet.build(s, run, per_tier=per_tier, seed=seed)}")


@app.command()
def stats():
    """Corpus counts and acceptance-figure comparison (baseline, pre-nickname run)."""
    from leadcore.patterns.matcher import match_detail
    from leadcore.report.workbook import overlap_stats

    with db.session(STATE["db"]) as s:
        n_obs = s.scalar(select(func.count()).select_from(EmailObservation))
        contacts = s.scalar(select(func.count()).select_from(Contact).where(Contact.origin == Origin.confirmed))
        generated = s.scalar(select(func.count()).select_from(Contact).where(Contact.origin == Origin.generated))
        sendable = s.scalar(select(func.count()).select_from(Contact.sendable().subquery()))
        ov = overlap_stats(s)
        reasons = s.execute(select(EmailObservation.excluded_reason, func.count())
                            .group_by(EmailObservation.excluded_reason)).all()
        # Row-level resolution over inference-eligible rows.
        res = {"exactly_one": 0, "ambiguous": 0, "unexplained": 0}
        for name, email, li in s.execute(select(EmailObservation.first_name_raw, EmailObservation.email,
                                                EmailObservation.linkedin_surname)
                                         .where(EmailObservation.excluded_reason.is_(None))):
            from leadcore.normalize.emails import parse
            k = len({m.pattern for m in match_detail(name, parse(email).match_local, last=li)})
            res["exactly_one" if k == 1 else "unexplained" if k == 0 else "ambiguous"] += 1
        base = pipeline.current_run(s, use_nicknames=False)
        nick = pipeline.current_run(s, use_nicknames=True)
        bc = pipeline.tier_counts(s, base) if base else {}
        nc = pipeline.tier_counts(s, nick) if nick else {}

    typer.echo(f"observations: {n_obs}")
    for r, n in sorted(reasons, key=lambda x: str(x[0])):
        typer.echo(f"  {r or 'included'}: {n}")
    typer.echo(f"confirmed contacts: {contacts}   generated: {generated}   sendable: {sendable}")
    rows_total = sum(res.values()) or 1
    measured = {
        "contacts": contacts, "domains": sum(bc.values()), "overlap": ov["business"],
        "exactly_one": res["exactly_one"] / rows_total,
        **{t: bc.get(t, 0) for t in "ABCD"}, "ABC": sum(bc.get(t, 0) for t in "ABC"),
    }
    typer.echo(f"row resolution: {res}")
    typer.echo("\nAcceptance (baseline, pre-nickname)      expected   measured    diff   within ±2%")
    for k, exp in ACCEPTANCE.items():
        got = measured[k]
        if k == "exactly_one":
            diff = (got - exp) * 100
            typer.echo(f"  {k:38} {exp:>8.0%} {got:>10.1%} {diff:+6.1f}pp  {'yes' if abs(diff) <= 2 else 'NO'}")
        else:
            diff = (got - exp) / exp * 100
            typer.echo(f"  {k:38} {exp:>8} {got:>10} {diff:+6.1f}%   {'yes' if abs(diff) <= 2 else 'NO'}")
    if nc:
        typer.echo(f"\nwith nickname pass: {_tiers_line(nc)}")
