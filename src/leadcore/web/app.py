"""Local web UI for leadcore.

A thin FastAPI layer over the existing package: it renders what the engine
already computes and lets you generate candidates, import verification results
from an external service, and export candidates for one. It performs no email
checks of its own.

Run with: ``leadcore web`` (see cli.py) or ``uvicorn leadcore.web.app:app``.
Intended for local use (127.0.0.1); it has no authentication.
"""

from __future__ import annotations

import io
from pathlib import Path

from fastapi import FastAPI, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select

from leadcore import config, db, pipeline, verification
from leadcore.generate.candidates import Target, generate
from leadcore.models import (Contact, DomainPattern, EmailObservation, InferenceRun, Origin,
                             VerificationResult)

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
app = FastAPI(title="leadcore")


def _current_run(s) -> InferenceRun | None:
    return pipeline.current_run(s, use_nicknames=True) or pipeline.current_run(s)


def _page(request: Request, name: str, **ctx) -> HTMLResponse:
    ctx["nav"] = NAV
    return TEMPLATES.TemplateResponse(request, name, ctx)


NAV = [("/", "Dashboard"), ("/firms", "Firms"), ("/generate", "Generate"),
       ("/verify", "Verification"), ("/validation", "Validation")]


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    with db.session() as s:
        run = _current_run(s)
        grades = pipeline.grade_counts(s, run) if run else {}
        tiers = {t: grades.get(t, 0) for t in ("A", "B", "C+", "C-", "D", "E")}
        contacts = s.scalar(select(func.count()).select_from(Contact).where(Contact.origin == Origin.confirmed))
        generated = s.scalar(select(func.count()).select_from(Contact).where(Contact.origin == Origin.generated))
        sendable = s.scalar(select(func.count()).select_from(Contact.sendable().subquery()))
        obs = s.scalar(select(func.count()).select_from(EmailObservation))
        by_source = dict(s.execute(select(EmailObservation.source, func.count())
                                   .group_by(EmailObservation.source)).all())
        vsumm = verification.summary(s)
        generatable = sum(grades.get(t, 0) for t in ("A", "B", "C+", "C-"))
    return _page(request, "dashboard.html", tiers=tiers, contacts=contacts, generated=generated,
                 sendable=sendable, obs=obs, by_source=by_source, run=run, vsumm=vsumm,
                 generatable=generatable)


@app.get("/firms", response_class=HTMLResponse)
def firms(request: Request, q: str = "", tier: str = ""):
    with db.session() as s:
        run = _current_run(s)
        if run is None:
            return _page(request, "firms.html", rows=[], q=q, tier=tier, total=0)
        stmt = select(DomainPattern).where(DomainPattern.run_id == run.id)
        if q:
            stmt = stmt.where(DomainPattern.domain.like(f"%{q.lower().strip()}%"))
        if tier:
            if tier in ("C+", "C-"):
                stmt = stmt.where(DomainPattern.subtier == tier)
            else:
                stmt = stmt.where(DomainPattern.tier == tier, DomainPattern.subtier.is_(None) | (DomainPattern.tier != "C"))
        total = s.scalar(select(func.count()).select_from(stmt.subquery()))
        rows = s.scalars(stmt.order_by(DomainPattern.tier, DomainPattern.contacts_held.desc(),
                                       DomainPattern.domain).limit(300)).all()
        rows = [_firm_row(r) for r in rows]
    return _page(request, "firms.html", rows=rows, q=q, tier=tier, total=total)


def _firm_row(dp: DomainPattern) -> dict:
    from leadcore.report.workbook import worked_example
    return {"domain": dp.domain, "grade": dp.subtier or dp.tier, "pattern": dp.pattern,
            "example": worked_example(dp.pattern, dp.domain) if dp.tier in "ABC" else "",
            "samples": dp.sample_count, "contacts": dp.contacts_held, "evidence": dp.evidence_email,
            "seen": dp.patterns_seen, "note": dp.note}


@app.get("/firms/{domain}", response_class=HTMLResponse)
def firm_detail(request: Request, domain: str):
    domain = domain.lower()
    with db.session() as s:
        run = _current_run(s)
        dp = s.scalars(select(DomainPattern).where(DomainPattern.run_id == run.id,
                                                   DomainPattern.domain == domain)).first() if run else None
        contacts = s.scalars(select(Contact).where(Contact.domain == domain)
                             .order_by(Contact.origin, Contact.email)).all()
        verify = verification.latest_status(s, [c.email for c in contacts])
    return _page(request, "firm_detail.html", domain=domain, dp=_firm_row(dp) if dp else None,
                 contacts=contacts, verify=verify)


@app.get("/generate", response_class=HTMLResponse)
def generate_form(request: Request):
    return _page(request, "generate.html", result=None, first="", last="", domain="")


@app.post("/generate", response_class=HTMLResponse)
def generate_run(request: Request, first: str = Form(...), last: str = Form(""), domain: str = Form(...)):
    with db.session() as s:
        run = _current_run(s)
        dp = None
        result = None
        if run is not None:
            dp = s.scalars(select(DomainPattern).where(DomainPattern.run_id == run.id,
                                                       DomainPattern.domain == domain.lower().strip())).first()
            [res] = generate(s, [Target(first.strip(), last.strip(), domain.strip())], run.id, persist=False)
            result = res
        verify = verification.latest_status(s, [c.address for c in (result.candidates if result else [])])
    return _page(request, "generate.html", result=result, dp=_firm_row(dp) if dp else None,
                 first=first, last=last, domain=domain, verify=verify)


@app.get("/verify", response_class=HTMLResponse)
def verify_page(request: Request, msg: str = ""):
    with db.session() as s:
        summ = verification.summary(s)
        recent = s.scalars(select(VerificationResult).order_by(VerificationResult.id.desc()).limit(25)).all()
    return _page(request, "verify.html", summ=summ, recent=recent, msg=msg,
                 services=sorted(verification.SERVICE_PRESETS))


@app.post("/verify/import")
async def verify_import(request: Request, file: UploadFile, service: str = Form("generic")):
    content = (await file.read()).decode("utf-8", errors="replace")
    with db.session() as s:
        st = verification.import_results(s, content, service=service, source_file=file.filename)
    msg = f"Imported {st.imported} of {st.rows} rows ({st.already_present} already present). " \
          + ", ".join(f"{k}={v}" for k, v in sorted(st.by_status.items()))
    return RedirectResponse(f"/verify?msg={msg}", status_code=303)


@app.get("/verify/export", response_class=PlainTextResponse)
def verify_export():
    with db.session() as s:
        run = _current_run(s)
        if run is None:
            return PlainTextResponse("no inference run yet", status_code=400)
        content, _ = verification.export_candidates(s, run)
    return PlainTextResponse(content, headers={
        "Content-Disposition": "attachment; filename=candidates_to_verify.csv"})


@app.get("/validation", response_class=HTMLResponse)
def validation(request: Request):
    path = config.DATA_OUT / "validation_sheet_apollo.xlsx"
    rows, summary = _read_validation(path)
    return _page(request, "validation.html", rows=rows, summary=summary, exists=path.exists())


def _read_validation(path: Path):
    if not path.exists():
        return [], {}
    from collections import Counter

    from openpyxl import load_workbook
    ws = load_workbook(path, read_only=True)["Validation"]
    header = None
    rows = []
    for r in ws.iter_rows(values_only=True):
        if header is None:
            header = list(r)
            continue
        rows.append(dict(zip(header, r)))
    by_tier = {}
    for t in ("A", "B", "C"):
        checked = [x for x in rows if x.get("Tier") == t and x.get("Verdict") in ("match", "mismatch")]
        m = sum(1 for x in checked if x.get("Verdict") == "match")
        by_tier[t] = (m, len(checked))
    return rows, by_tier
