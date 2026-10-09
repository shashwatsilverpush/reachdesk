"""Local web UI for leadcore.

A thin FastAPI layer over the existing package: it renders what the engine
already computes, lets you generate candidates, import verification results
from an external service, and run live SMTP verification via the email_verifier
pipeline.

Run with: ``leadcore web`` (see cli.py) or ``uvicorn leadcore.web.app:app``.
Intended for local use (127.0.0.1); it has no authentication.
"""

from __future__ import annotations

import io
import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent.parent.parent / ".env")

import dns.resolver
import httpx

from fastapi import FastAPI, Form, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select

from leadcore import config, db, pipeline, verification
from leadcore.generate.candidates import Target, generate
from leadcore.models import (Contact, DomainPattern, EmailObservation, InferenceRun, Origin,
                             VerificationResult)

_ev_root = str(Path(__file__).resolve().parent.parent.parent.parent / "email_verifier")
if _ev_root not in sys.path:
    sys.path.insert(0, _ev_root)
try:
    from email_verifier.pipeline import verify_email as smtp_verify
    from email_verifier.ms_probe import is_m365_mx, m365_pattern_search
    from email_verifier.dnsx import get_mx_hosts_cached
    from email_verifier.ext_verify import ext_verify
    HAS_EMAIL_VERIFIER = True
except ImportError:
    HAS_EMAIL_VERIFIER = False

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


_STRIP_SUFFIXES = re.compile(
    r"\b(Inc|Corp|Corporation|Ltd|Limited|LLC|LLP|Co|Company|Group|Holdings|"
    r"Technologies|Technology|Tech|Solutions|Services|Consulting|International|Global)\b\.?",
    re.IGNORECASE,
)

APOLLO_API_KEY = os.environ.get("APOLLO_API_KEY", "")


def _extract_slug(url: str) -> str:
    """Extract the raw LinkedIn slug from a profile URL."""
    parsed = urlparse(url.strip())
    path = parsed.path.rstrip("/")
    m = re.match(r"^/in/([^/]+)$", path)
    return m.group(1).lower() if m else ""


def _parse_linkedin_slug(url: str) -> tuple[str, str]:
    """Fallback: extract (first, last) from the URL slug alone."""
    slug = _extract_slug(url)
    if not slug:
        return "", ""
    parts = slug.split("-")
    if len(parts) > 2 and re.fullmatch(r"[0-9a-f]{6,}", parts[-1]):
        parts = parts[:-1]
    elif len(parts) > 2 and re.fullmatch(r"[0-9a-z]{8,}", parts[-1]) and any(c.isdigit() for c in parts[-1]):
        parts = parts[:-1]
    if not parts:
        return "", ""
    return parts[0].title(), " ".join(p.title() for p in parts[1:]) if len(parts) > 1 else ""


def _apollo_lookup(linkedin_url: str) -> dict:
    """Look up a LinkedIn profile via Apollo's people/match API."""
    if not APOLLO_API_KEY:
        return {}
    try:
        with httpx.Client(timeout=15) as client:
            resp = client.post(
                "https://api.apollo.io/v1/people/match",
                json={"linkedin_url": linkedin_url},
                headers={"x-api-key": APOLLO_API_KEY, "Content-Type": "application/json"},
            )
            if resp.status_code != 200:
                return {}
            data = resp.json().get("person") or {}
    except Exception:
        return {}

    info: dict[str, str] = {}
    info["first"] = data.get("first_name", "")
    info["last"] = data.get("last_name", "")
    info["role"] = data.get("title", "")

    org = data.get("organization") or {}
    info["company"] = org.get("name", "") or data.get("organization_name", "")
    info["domain"] = org.get("primary_domain", "") or org.get("website_url", "")
    if info["domain"]:
        info["domain"] = re.sub(r"^https?://", "", info["domain"]).rstrip("/").split("/")[0]

    return info


def _guess_domain(company: str) -> str:
    """Try common domain patterns for a company name, verified via DNS."""
    if not company:
        return ""
    clean = _STRIP_SUFFIXES.sub("", company).strip().strip(",").strip()
    words = clean.lower().split()
    if not words:
        return ""

    candidates = [
        "".join(words) + ".com",
        "-".join(words) + ".com",
    ]
    if len(words) > 1:
        candidates.append(words[0] + ".com")
    candidates += ["".join(words) + s for s in (".io", ".co", ".ai", ".org")]

    resolver = dns.resolver.Resolver()
    resolver.timeout = 3
    resolver.lifetime = 3
    for domain in candidates:
        try:
            resolver.resolve(domain, "A")
            return domain
        except Exception:
            continue
    return ""


def _recover_last_from_slug(slug: str, first: str, last_initial: str) -> str:
    """Recover full last name from a LinkedIn slug when Apollo only returned an initial.

    Handles common slug formats: flast (dtsauder), firstlast (dennistsauder).
    Hyphenated slugs (dennis-tsauder) are already split correctly by _parse_linkedin_slug.
    """
    if not slug or not first or not last_initial:
        return ""
    slug = re.sub(r"[0-9]+$", "", slug)
    fi = first[0].lower()
    li = last_initial.lower()

    if slug.startswith(fi) and len(slug) > 1 and slug[1] == li:
        candidate = slug[1:]
        if len(candidate) >= 2:
            return candidate.title()

    fl = first.lower()
    if slug.startswith(fl) and len(slug) > len(fl) and slug[len(fl)] == li:
        candidate = slug[len(fl):]
        if len(candidate) >= 2:
            return candidate.title()

    return ""


def _linkedin_lookup(url: str) -> dict:
    """Resolve a LinkedIn URL to first, last, company, role, domain."""
    slug_first, slug_last = _parse_linkedin_slug(url)
    info = _apollo_lookup(url)
    if info.get("first"):
        if len(info.get("last", "")) <= 1:
            slug = _extract_slug(url)
            recovered = _recover_last_from_slug(slug, info["first"], info.get("last", ""))
            if recovered:
                info["last"] = recovered
        if not info.get("domain") and info.get("company"):
            info["domain"] = _guess_domain(info["company"])

        warnings = []
        if slug_last:
            apollo_name = f"{info['first']} {info.get('last', '')}".strip().lower()
            slug_name = f"{slug_first} {slug_last}".strip().lower()
            if apollo_name != slug_name:
                info["slug_name"] = f"{slug_first} {slug_last}".strip()
                warnings.append(f"LinkedIn slug suggests \"{info['slug_name']}\" — verify name is correct")
        if info.get("company"):
            warnings.append("Company data from Apollo — may not reflect current employer")
        if warnings:
            info["warnings"] = warnings
        info["source"] = "apollo"
        return info

    return {"first": slug_first, "last": slug_last, "company": "", "role": "", "domain": "",
            "source": "slug"}
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


@app.get("/generate/linkedin-lookup", response_class=JSONResponse)
def linkedin_lookup_endpoint(url: str = Query("")):
    """AJAX endpoint: resolve a LinkedIn profile to name + company + domain."""
    url = url.strip()
    if not url or "/in/" not in url:
        return JSONResponse({"error": "Not a LinkedIn profile URL"}, status_code=400)
    if not url.startswith("http"):
        url = "https://" + url

    return _linkedin_lookup(url)


@app.get("/generate", response_class=HTMLResponse)
def generate_form(request: Request):
    return _page(request, "generate.html", result=None, first="", last="", domain="",
                 linkedin_url="", has_smtp_verify=HAS_EMAIL_VERIFIER, live_verify={},
                 pattern_hits=[])


@app.post("/generate", response_class=HTMLResponse)
def generate_run(request: Request, first: str = Form(""), last: str = Form(""),
                 domain: str = Form(""), verify_now: str = Form(""),
                 linkedin_url: str = Form("")):
    if linkedin_url.strip():
        li_url = linkedin_url.strip()
        if not li_url.startswith("http"):
            li_url = "https://" + li_url
        if not first.strip() or not domain.strip():
            info = _linkedin_lookup(li_url)
            first = first or info.get("first", "")
            last = last or info.get("last", "")
            if not domain.strip():
                domain = info.get("domain", "")

    if not first.strip():
        return _page(request, "generate.html", result=None, first=first, last=last,
                     domain=domain, linkedin_url=linkedin_url,
                     has_smtp_verify=HAS_EMAIL_VERIFIER, live_verify={},
                     error="Could not extract a name. Enter the name manually.")

    if not domain.strip():
        return _page(request, "generate.html", result=None, first=first, last=last,
                     domain=domain, linkedin_url=linkedin_url,
                     has_smtp_verify=HAS_EMAIL_VERIFIER, live_verify={},
                     error="Could not detect the firm domain. Enter it manually.")

    live_verify: dict[str, dict] = {}
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

    pattern_hits: list[dict] = []
    not_generatable = result and result.status == "not_generatable"

    if not_generatable and HAS_EMAIL_VERIFIER and last.strip():
        from email_verifier.ms_probe import _generate_patterns
        domain_is_m365 = False
        try:
            mx_hosts = get_mx_hosts_cached(domain.strip())
            domain_is_m365 = any(is_m365_mx(h) for h in mx_hosts)
        except Exception:
            pass

        if domain_is_m365:
            hits = m365_pattern_search(first.strip(), last.strip(), domain.strip(), timeout=10)
            pattern_hits = [
                {"email": h.email, "pattern": h.pattern, "exists": h.exists}
                for h in hits
            ]
        else:
            candidates = _generate_patterns(first.strip(), last.strip(), domain.strip())
            for email, label in candidates:
                r = smtp_verify(email, timeout=10, retries=1, delay=2, detect_catch_all=True)
                d = r.to_dict()
                live_verify[email] = d
                pattern_hits.append({
                    "email": email, "pattern": label,
                    "exists": True if d["verdict"] == "valid" else
                              (False if d["verdict"] == "invalid" else None),
                })
            if pattern_hits and all(h["exists"] is None for h in pattern_hits):
                for h in pattern_hits:
                    ev = ext_verify(h["email"], timeout=15)
                    if ev.status == "valid":
                        h["exists"] = True
                        h["ext_service"] = ev.service
                    elif ev.status == "invalid":
                        h["exists"] = False
                        h["ext_service"] = ev.service

    elif verify_now and HAS_EMAIL_VERIFIER and result and result.candidates:
        for c in result.candidates:
            r = smtp_verify(c.address, timeout=10, retries=1, delay=2, detect_catch_all=True)
            live_verify[c.address] = r.to_dict()

        all_catch_all = all(
            live_verify.get(c.address, {}).get("reason", "") == "catch_all_domain"
            for c in result.candidates
        )
        none_confirmed = all(
            live_verify.get(c.address, {}).get("verdict") in ("invalid", "risky", "unknown")
            for c in result.candidates
        )

        if all_catch_all and not any(
            "m365" in live_verify.get(c.address, {}).get("reason", "")
            for c in result.candidates
        ):
            domain_is_m365 = False
            try:
                mx_hosts = get_mx_hosts_cached(domain.strip())
                domain_is_m365 = any(is_m365_mx(h) for h in mx_hosts)
            except Exception:
                pass
            if not domain_is_m365:
                for c in result.candidates:
                    ev = ext_verify(c.address, timeout=15)
                    lv = live_verify[c.address]
                    if ev.status == "valid":
                        lv["verdict"] = "valid"
                        lv["reason"] = f"confirmed_by_{ev.service}"
                    elif ev.status == "invalid":
                        lv["verdict"] = "invalid"
                        lv["reason"] = f"rejected_by_{ev.service}"

        none_confirmed = all(
            live_verify.get(c.address, {}).get("verdict") in ("invalid", "risky", "unknown")
            for c in result.candidates
        )
        if none_confirmed and last.strip():
            already = {c.address for c in result.candidates}
            any_m365 = any(
                "m365" in live_verify.get(c.address, {}).get("reason", "")
                for c in result.candidates
            )
            domain_is_m365 = any_m365
            if not any_m365:
                try:
                    mx_hosts = get_mx_hosts_cached(domain.strip())
                    domain_is_m365 = any(is_m365_mx(h) for h in mx_hosts)
                except Exception:
                    pass

            if domain_is_m365:
                hits = m365_pattern_search(first.strip(), last.strip(), domain.strip(), timeout=10)
                pattern_hits = [
                    {"email": h.email, "pattern": h.pattern, "exists": h.exists}
                    for h in hits if h.email not in already
                ]
            else:
                from email_verifier.ms_probe import _generate_patterns
                candidates = _generate_patterns(first.strip(), last.strip(), domain.strip())
                for email, label in candidates:
                    if email in already:
                        continue
                    ev = ext_verify(email, timeout=15)
                    if ev.status == "valid":
                        pattern_hits.append({
                            "email": email, "pattern": label,
                            "exists": True, "ext_service": ev.service,
                        })
                    elif ev.status == "invalid":
                        pattern_hits.append({
                            "email": email, "pattern": label,
                            "exists": False, "ext_service": ev.service,
                        })
                    else:
                        pattern_hits.append({
                            "email": email, "pattern": label,
                            "exists": None, "ext_service": ev.service,
                        })

    return _page(request, "generate.html", result=result, dp=_firm_row(dp) if dp else None,
                 first=first, last=last, domain=domain, linkedin_url=linkedin_url, verify=verify,
                 has_smtp_verify=HAS_EMAIL_VERIFIER, live_verify=live_verify,
                 pattern_hits=pattern_hits)


@app.get("/verify", response_class=HTMLResponse)
def verify_page(request: Request, msg: str = ""):
    with db.session() as s:
        summ = verification.summary(s)
        recent = s.scalars(select(VerificationResult).order_by(VerificationResult.id.desc()).limit(25)).all()
    return _page(request, "verify.html", summ=summ, recent=recent, msg=msg,
                 services=sorted(verification.SERVICE_PRESETS),
                 has_smtp_verify=HAS_EMAIL_VERIFIER, smtp_results=None, smtp_input="")


@app.post("/verify/check", response_class=HTMLResponse)
def verify_check(request: Request, emails: str = Form(""), timeout: int = Form(10)):
    """Run live SMTP verification on pasted email addresses."""
    lines = [ln.strip() for ln in emails.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    smtp_results = []
    if HAS_EMAIL_VERIFIER and lines:
        for addr in lines[:50]:
            r = smtp_verify(addr, timeout=timeout, retries=1, delay=2, detect_catch_all=True)
            smtp_results.append(r.to_dict())
    with db.session() as s:
        summ = verification.summary(s)
        recent = s.scalars(select(VerificationResult).order_by(VerificationResult.id.desc()).limit(25)).all()
    return _page(request, "verify.html", summ=summ, recent=recent, msg="",
                 services=sorted(verification.SERVICE_PRESETS),
                 has_smtp_verify=HAS_EMAIL_VERIFIER, smtp_results=smtp_results, smtp_input=emails)


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
