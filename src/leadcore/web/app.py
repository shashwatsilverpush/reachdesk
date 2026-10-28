"""LeadCore CRM — FastAPI web application.

Run with: ``leadcore web`` (see cli.py) or ``uvicorn leadcore.web.app:app``.
"""

from __future__ import annotations

import base64
import csv
import io
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
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
from sqlalchemy.orm import joinedload

from leadcore import config, db, pipeline, verification
from leadcore.generate.candidates import Target, generate
from leadcore.models import (
    CRMUser, Contact, DomainPattern, EmailObservation, InferenceRun,
    Lead, LeadActivity, LeadSequence, LeadStatus, MailTemplate,
    Origin, Sequence, SequenceEmail, SequenceEmailStatus, SequenceStep,
    UserRole, VerificationResult,
)
from leadcore.web.auth import (
    SSO_ENABLED,
    build_authorize_url, create_session_cookie, create_sso_state_cookie,
    exchange_code, fetch_userinfo, get_current_user, hash_password,
    read_sso_state_cookie, verify_password,
)

# ---------------------------------------------------------------------------
# Email-verifier (optional package)
# ---------------------------------------------------------------------------

_ev_root = str(Path(__file__).resolve().parent.parent.parent.parent / "email_verifier")
if _ev_root not in sys.path:
    sys.path.insert(0, _ev_root)
try:
    from email_verifier.pipeline import verify_email as smtp_verify
    from email_verifier.ms_probe import is_m365_mx, m365_pattern_search
    from email_verifier.dnsx import get_mx_hosts_cached
    from email_verifier.ext_verify import ext_verify, ext_verify_patterns, get_credits
    HAS_EMAIL_VERIFIER = True
except ImportError:
    HAS_EMAIL_VERIFIER = False

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
APOLLO_API_KEY = os.environ.get("APOLLO_API_KEY", "")

NAV = [
    ("/", "Dashboard", "📊"),
    ("/leads", "Leads", "☰"),
    ("/new-lead", "New Lead", "+"),
    ("/scope", "Find Email", "🔍"),
    ("/sequences", "Sequences", "✉"),
    ("/settings/team", "Settings", "⚙"),
]

app = FastAPI(title="ReachDesk")


@app.on_event("startup")
def _ensure_db():
    """Create tables if running without alembic (e.g. fresh container)."""
    from leadcore.models import Base
    eng = db.engine()
    Base.metadata.create_all(eng)


_LEAD_STATUSES = [s.value for s in LeadStatus]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _page(request: Request, name: str, **ctx) -> HTMLResponse:
    ctx.setdefault("nav", NAV)
    ctx.setdefault("current_user", get_current_user(request))
    return TEMPLATES.TemplateResponse(request, name, ctx)


def _require_user(request: Request) -> CRMUser | None:
    user = get_current_user(request)
    if not user:
        return None
    return user


def _is_admin(user: CRMUser) -> bool:
    return user.is_admin or user.role == UserRole.admin.value

_USER_ROLES = [r.value for r in UserRole]


def _visible_user_ids(s, user: CRMUser) -> list[int] | None:
    """Return list of user IDs whose leads this user can see, or None for all."""
    if _is_admin(user):
        return None
    if user.role == UserRole.manager.value:
        report_ids = list(s.scalars(
            select(CRMUser.id).where(CRMUser.manager_id == user.id)
        ))
        return [user.id] + report_ids
    return [user.id]


def _current_run(s) -> InferenceRun | None:
    return pipeline.current_run(s, use_nicknames=True) or pipeline.current_run(s)


def _firm_row(dp: DomainPattern) -> dict:
    from leadcore.report.workbook import worked_example
    return {
        "domain": dp.domain, "grade": dp.subtier or dp.tier, "pattern": dp.pattern,
        "example": worked_example(dp.pattern, dp.domain) if dp.tier in "ABC" else "",
        "samples": dp.sample_count, "contacts": dp.contacts_held, "evidence": dp.evidence_email,
        "seen": dp.patterns_seen, "note": dp.note,
    }


def _log_activity(s, lead_id: int, user_id: int | None, action: str, detail: str | None = None):
    s.add(LeadActivity(lead_id=lead_id, user_id=user_id, action=action, detail=detail))


def _render_vars(text: str, lead: Lead) -> str:
    replacements = {
        "{{first_name}}": lead.first_name or "",
        "{{last_name}}": lead.last_name or "",
        "{{full_name}}": f"{lead.first_name or ''} {lead.last_name or ''}".strip(),
        "{{email}}": lead.email or "",
        "{{company}}": lead.company or "",
        "{{domain}}": lead.domain or "",
        "{{role}}": lead.role_title or "",
        "{{phone}}": lead.phone or "",
        "{{country}}": lead.country or "",
    }
    for k, v in replacements.items():
        text = text.replace(k, v)
    return text


# ---------------------------------------------------------------------------
# LinkedIn helpers
# ---------------------------------------------------------------------------

_STRIP_SUFFIXES = re.compile(
    r"\b(Inc|Corp|Corporation|Ltd|Limited|LLC|LLP|Co|Company|Group|Holdings|"
    r"Technologies|Technology|Tech|Solutions|Services|Consulting|International|Global)\b\.?",
    re.IGNORECASE,
)


def _extract_slug(url: str) -> str:
    parsed = urlparse(url.strip())
    path = parsed.path.rstrip("/")
    m = re.match(r"^/in/([^/]+)$", path)
    return m.group(1).lower() if m else ""


def _parse_linkedin_slug(url: str) -> tuple[str, str]:
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
    if not company:
        return ""
    clean = _STRIP_SUFFIXES.sub("", company).strip().strip(",").strip()
    words = clean.lower().split()
    if not words:
        return ""
    candidates = ["".join(words) + ".com", "-".join(words) + ".com"]
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


def _extract_middle_from_slug(url: str, first: str, last: str) -> str:
    """Compare LinkedIn slug parts with known first/last to recover a middle name."""
    slug = _extract_slug(url)
    if not slug:
        return ""
    parts = slug.split("-")
    if len(parts) > 2 and re.fullmatch(r"[0-9a-f]{6,}", parts[-1]):
        parts = parts[:-1]
    elif len(parts) > 2 and re.fullmatch(r"[0-9a-z]{8,}", parts[-1]) and any(c.isdigit() for c in parts[-1]):
        parts = parts[:-1]
    if len(parts) < 3:
        return ""
    fl = first.lower().strip()
    ll = last.lower().strip()
    if parts[0].lower() == fl and parts[-1].lower() == ll:
        middle_parts = parts[1:-1]
        return " ".join(p.title() for p in middle_parts)
    return ""


def _linkedin_lookup(url: str) -> dict:
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
        # Recover middle name from slug
        middle = _extract_middle_from_slug(url, info["first"], info.get("last", ""))
        if middle:
            info["first"] = f"{info['first']} {middle}"
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


# ---------------------------------------------------------------------------
# Generation helper (used by /new-lead and inline verify)
# ---------------------------------------------------------------------------

def _run_generation(first: str, last: str, domain: str, linkedin_url: str = "",
                    do_verify: bool = False):
    """Core generate logic. Returns (result, dp, pattern_hits, best_hit, live_verify, ext_credit_info)."""
    live_verify: dict[str, dict] = {}
    pattern_hits: list[dict] = []
    ext_credit_info: dict | None = None
    dp = None
    result = None

    with db.session() as s:
        run = _current_run(s)
        if run is not None:
            dp_row = s.scalars(select(DomainPattern).where(
                DomainPattern.run_id == run.id,
                DomainPattern.domain == domain.lower().strip(),
            )).first()
            if dp_row:
                dp = _firm_row(dp_row)
            [res] = generate(s, [Target(first.strip(), last.strip(), domain.strip())], run.id, persist=False)
            result = res

    not_generatable = result is None or (result and result.status == "not_generatable")
    no_candidates = not result or not getattr(result, 'candidates', None)

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
            pattern_hits = [{"email": h.email, "pattern": h.pattern, "exists": h.exists} for h in hits]
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
            any_rejected = any(h["exists"] is False for h in pattern_hits)
            all_unknown = all(h["exists"] is None for h in pattern_hits)
            smtp_unreliable = not any_rejected and not all_unknown
            if pattern_hits and (all_unknown or smtp_unreliable):
                if smtp_unreliable:
                    for h in pattern_hits:
                        if h["exists"] is True:
                            h["exists"] = None
                ext_credit_info = ext_verify_patterns(pattern_hits, timeout=15)

    elif HAS_EMAIL_VERIFIER and result and result.candidates and do_verify:
        for c in result.candidates:
            r = smtp_verify(c.address, timeout=10, retries=1, delay=2, detect_catch_all=True)
            live_verify[c.address] = r.to_dict()

        all_catch_all = all(
            live_verify.get(c.address, {}).get("reason", "") == "catch_all_domain"
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
                    pattern_hits.append({"email": email, "pattern": label, "exists": None})
                ext_credit_info = ext_verify_patterns(pattern_hits, timeout=15)

    best_hit = None
    if pattern_hits:
        from email_verifier.ms_probe import PATTERN_SPECIFICITY
        for h in pattern_hits:
            h["specificity"] = PATTERN_SPECIFICITY.get(h["pattern"], 0)
        confirmed = [h for h in pattern_hits if h.get("exists") is True]
        if confirmed:
            confirmed.sort(key=lambda h: -h["specificity"])
            best_hit = confirmed[0]
            max_spec = best_hit["specificity"]
            if max_spec >= 3:
                best_hit["confidence"] = "high"
                best_hit["confidence_note"] = "Uses full first + last name — unique to this person."
            elif max_spec == 2:
                best_hit["confidence"] = "medium"
                best_hit["confidence_note"] = "Uses a partial name — likely correct but could match another person with similar name parts."
            else:
                best_hit["confidence"] = "low"
                best_hit["confidence_note"] = "Only generic patterns matched. These may belong to a different person at this company."

    return result, dp, pattern_hits, best_hit, live_verify, ext_credit_info


# ===================================================================
#  AUTH ROUTES
# ===================================================================

@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return TEMPLATES.TemplateResponse(request, "login.html", {"error": "", "sso_enabled": SSO_ENABLED})


@app.post("/login", response_class=HTMLResponse)
def login_submit(request: Request, email: str = Form(""), password: str = Form("")):
    with db.session() as s:
        user = s.scalars(select(CRMUser).where(CRMUser.email == email.strip().lower())).first()
        if not user or not user.password_hash or not verify_password(password, user.password_hash):
            return TEMPLATES.TemplateResponse(request, "login.html",
                                              {"error": "Invalid email or password", "email": email, "sso_enabled": SSO_ENABLED})
        if not user.active:
            return TEMPLATES.TemplateResponse(request, "login.html",
                                              {"error": "Account deactivated", "email": email, "sso_enabled": SSO_ENABLED})
        cookie = create_session_cookie(user.id)
    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie("leadcore_session", cookie, httponly=True, max_age=7 * 86400,
                     samesite="lax", secure=config.SSO_REDIRECT_URI.startswith("https"))
    return resp


@app.get("/register", response_class=HTMLResponse)
def register_page(request: Request):
    return TEMPLATES.TemplateResponse(request, "register.html", {"error": "", "name": "", "email": ""})


@app.post("/register", response_class=HTMLResponse)
def register_submit(request: Request, name: str = Form(""), email: str = Form(""),
                    password: str = Form(""), confirm: str = Form("")):
    ctx = {"name": name, "email": email}
    if not name.strip() or not email.strip():
        return TEMPLATES.TemplateResponse(request, "register.html", {**ctx, "error": "Name and email are required"})
    if len(password) < 6:
        return TEMPLATES.TemplateResponse(request, "register.html", {**ctx, "error": "Password must be at least 6 characters"})
    if password != confirm:
        return TEMPLATES.TemplateResponse(request, "register.html", {**ctx, "error": "Passwords do not match"})

    with db.session() as s:
        existing = s.scalars(select(CRMUser).where(CRMUser.email == email.strip().lower())).first()
        if existing:
            if existing.password_hash:
                return TEMPLATES.TemplateResponse(request, "register.html", {**ctx, "error": "Email already registered"})
            existing.password_hash = hash_password(password)
            existing.name = name.strip()
            uid = existing.id
        else:
            is_first = s.scalar(select(func.count()).select_from(CRMUser).where(CRMUser.password_hash.isnot(None))) == 0
            user = CRMUser(
                name=name.strip(),
                email=email.strip().lower(),
                password_hash=hash_password(password),
                is_admin=is_first,
                active=True,
            )
            s.add(user)
            s.flush()
            uid = user.id

    cookie = create_session_cookie(uid)
    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie("leadcore_session", cookie, httponly=True, max_age=7 * 86400,
                     samesite="lax", secure=config.SSO_REDIRECT_URI.startswith("https"))
    return resp


@app.post("/logout")
def logout():
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie("leadcore_session")
    return resp


# ===================================================================
#  SSO — OIDC Authorization Code + PKCE via auth.silverpush.live
# ===================================================================

@app.get("/auth/sso")
def sso_initiate():
    if not SSO_ENABLED:
        return RedirectResponse("/login", status_code=302)
    url, state_data = build_authorize_url()
    resp = RedirectResponse(url, status_code=302)
    _secure = config.SSO_REDIRECT_URI.startswith("https")
    resp.set_cookie("sso_state", create_sso_state_cookie(state_data),
                     httponly=True, max_age=300, samesite="lax", secure=_secure)
    return resp


@app.get("/auth/sso/callback", response_class=HTMLResponse)
def sso_callback(request: Request, code: str = "", state: str = "", error: str = ""):
    try:
        return _sso_callback_inner(request, code, state, error)
    except Exception as exc:
        import traceback
        traceback.print_exc()
        return TEMPLATES.TemplateResponse(request, "login.html",
                                          {"error": f"SSO error: {exc}", "sso_enabled": SSO_ENABLED})


def _sso_callback_inner(request: Request, code: str, state: str, error: str):
    if error or not code:
        return TEMPLATES.TemplateResponse(request, "login.html",
                                          {"error": f"SSO failed: {error or 'no code returned'}", "sso_enabled": SSO_ENABLED})

    cookie = request.cookies.get("sso_state", "")
    state_data = read_sso_state_cookie(cookie) if cookie else None
    if not state_data or state_data.get("state") != state:
        if not cookie:
            detail = "SSO failed: state cookie missing — your browser may be blocking cookies"
        elif not state_data:
            detail = "SSO failed: state cookie expired or invalid — please try again"
        else:
            detail = "SSO state mismatch — please try again"
        return TEMPLATES.TemplateResponse(request, "login.html",
                                          {"error": detail, "sso_enabled": SSO_ENABLED})

    tokens, token_err = exchange_code(code, state_data["verifier"])
    if not tokens or "access_token" not in tokens:
        return TEMPLATES.TemplateResponse(request, "login.html",
                                          {"error": f"SSO token exchange failed: {token_err}", "sso_enabled": SSO_ENABLED})

    userinfo = fetch_userinfo(tokens["access_token"])
    if not userinfo or not userinfo.get("email"):
        return TEMPLATES.TemplateResponse(request, "login.html",
                                          {"error": "Could not retrieve user info from SSO", "sso_enabled": SSO_ENABLED})

    email = userinfo["email"].strip().lower()
    name = userinfo.get("name", email.split("@")[0])

    with db.session() as s:
        user = s.scalars(select(CRMUser).where(CRMUser.email == email)).first()
        if user:
            if not user.active:
                return TEMPLATES.TemplateResponse(request, "login.html",
                                                  {"error": "Account deactivated", "sso_enabled": SSO_ENABLED})
            uid = user.id
        else:
            is_first = s.scalar(select(func.count()).select_from(CRMUser).where(CRMUser.password_hash.isnot(None))) == 0
            user = CRMUser(
                name=name,
                email=email,
                is_admin=is_first,
                active=True,
            )
            s.add(user)
            s.flush()
            uid = user.id

    session_cookie = create_session_cookie(uid)
    resp = RedirectResponse("/", status_code=303)
    _secure = config.SSO_REDIRECT_URI.startswith("https")
    resp.set_cookie("leadcore_session", session_cookie, httponly=True, max_age=7 * 86400,
                     samesite="lax", secure=_secure)
    resp.delete_cookie("sso_state")
    return resp


# ===================================================================
#  DASHBOARD
# ===================================================================

@app.get("/", response_class=HTMLResponse)
def dashboard_page(request: Request):
    user = _require_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)

    now = _now()
    week_ago = now - timedelta(days=7)
    month_ago = now - timedelta(days=30)

    with db.session() as s:
        vis = _visible_user_ids(s, user)

        base = select(func.count()).select_from(Lead)
        if vis is not None:
            base = base.where(Lead.assigned_to.in_(vis))
        total_leads = s.scalar(base) or 0

        pipeline_counts: dict[str, int] = {}
        for st in LeadStatus:
            cnt_stmt = select(func.count()).select_from(Lead).where(Lead.status == st)
            if vis is not None:
                cnt_stmt = cnt_stmt.where(Lead.assigned_to.in_(vis))
            pipeline_counts[st.value] = s.scalar(cnt_stmt) or 0

        week_stmt = select(func.count()).select_from(Lead).where(Lead.created_at >= week_ago)
        month_stmt = select(func.count()).select_from(Lead).where(Lead.created_at >= month_ago)
        if vis is not None:
            week_stmt = week_stmt.where(Lead.assigned_to.in_(vis))
            month_stmt = month_stmt.where(Lead.assigned_to.in_(vis))
        leads_this_week = s.scalar(week_stmt) or 0
        leads_this_month = s.scalar(month_stmt) or 0

        team_q = (
            select(CRMUser.name, func.count(Lead.id))
            .outerjoin(Lead, Lead.assigned_to == CRMUser.id)
            .where(CRMUser.active.is_(True))
        )
        if vis is not None:
            team_q = team_q.where(CRMUser.id.in_(vis))
        team_stats = s.execute(
            team_q.group_by(CRMUser.id, CRMUser.name)
            .order_by(func.count(Lead.id).desc())
        ).all()

        act_stmt = (
            select(LeadActivity)
            .options(joinedload(LeadActivity.lead), joinedload(LeadActivity.user))
        )
        if vis is not None:
            act_stmt = act_stmt.join(Lead, LeadActivity.lead_id == Lead.id).where(Lead.assigned_to.in_(vis))
        activities = s.scalars(
            act_stmt.order_by(LeadActivity.created_at.desc()).limit(20)
        ).unique().all()

    return _page(request, "dashboard.html",
                 total_leads=total_leads,
                 pipeline=pipeline_counts,
                 leads_this_week=leads_this_week,
                 leads_this_month=leads_this_month,
                 team_stats=team_stats,
                 activities=activities)


# ===================================================================
#  LEADS
# ===================================================================

@app.get("/leads", response_class=HTMLResponse)
def leads_page(request: Request, status: str = "", q: str = "", sort: str = ""):
    user = _require_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)

    with db.session() as s:
        vis = _visible_user_ids(s, user)

        pipeline_counts = {}
        for st in LeadStatus:
            cnt_stmt = select(func.count()).select_from(Lead).where(Lead.status == st)
            if vis is not None:
                cnt_stmt = cnt_stmt.where(Lead.assigned_to.in_(vis))
            pipeline_counts[st.value] = s.scalar(cnt_stmt) or 0
        total_leads = sum(pipeline_counts.values())

        stmt = select(Lead)
        if vis is not None:
            stmt = stmt.where(Lead.assigned_to.in_(vis))
        if status:
            stmt = stmt.where(Lead.status == LeadStatus(status))
        if q:
            pattern = f"%{q.strip()}%"
            stmt = stmt.where(
                Lead.first_name.ilike(pattern) | Lead.last_name.ilike(pattern)
                | Lead.company.ilike(pattern) | Lead.domain.ilike(pattern)
                | Lead.email.ilike(pattern)
            )

        order = Lead.created_at.desc()
        if sort == "name":
            order = Lead.first_name.asc()
        elif sort == "company":
            order = Lead.company.asc()
        elif sort == "status":
            order = Lead.status.asc()
        elif sort == "activity":
            order = Lead.last_activity_at.desc().nullslast()

        leads = s.scalars(
            stmt.options(joinedload(Lead.assigned_user))
            .order_by(order).limit(500)
        ).unique().all()

    return _page(request, "leads.html", leads=leads, pipeline=pipeline_counts,
                 total_leads=total_leads, statuses=_LEAD_STATUSES,
                 filt_status=status, filt_q=q, filt_sort=sort)


@app.get("/leads/{lead_id}", response_class=HTMLResponse)
def lead_detail(request: Request, lead_id: int):
    user = _require_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)

    with db.session() as s:
        lead = s.get(Lead, lead_id)
        if not lead:
            return RedirectResponse("/leads", status_code=302)

        users = s.scalars(select(CRMUser).where(CRMUser.active.is_(True)).order_by(CRMUser.name)).all()
        sequences = s.scalars(select(Sequence).order_by(Sequence.name)).all()

        lead_seq = s.scalars(
            select(LeadSequence)
            .options(joinedload(LeadSequence.sequence), joinedload(LeadSequence.emails).joinedload(SequenceEmail.step))
            .where(LeadSequence.lead_id == lead_id)
        ).unique().first()

        activities = s.scalars(
            select(LeadActivity)
            .options(joinedload(LeadActivity.user))
            .where(LeadActivity.lead_id == lead_id)
            .order_by(LeadActivity.created_at.desc())
            .limit(50)
        ).unique().all()

        dp = None
        if lead.domain:
            run = _current_run(s)
            if run:
                dp_row = s.scalars(select(DomainPattern).where(
                    DomainPattern.run_id == run.id, DomainPattern.domain == lead.domain
                )).first()
                if dp_row:
                    dp = _firm_row(dp_row)

    can_delete = _is_admin(user) or user.role == UserRole.manager.value
    return _page(request, "lead_detail.html", lead=lead, users=users, statuses=_LEAD_STATUSES,
                 sequences=sequences, lead_seq=lead_seq, activities=activities, dp=dp,
                 can_delete=can_delete)


@app.post("/leads", response_class=HTMLResponse)
def lead_create(request: Request, first_name: str = Form(""), last_name: str = Form(""),
                email: str = Form(""), domain: str = Form(""), company: str = Form(""),
                linkedin_url: str = Form(""), role_title: str = Form(""),
                phone: str = Form(""), country: str = Form(""),
                assigned_to: str = Form(""), notes: str = Form("")):
    user = _require_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)

    clean_email = email.strip().lower() or None
    clean_li = linkedin_url.strip() or None

    with db.session() as s:
        if clean_email:
            dup = s.scalars(
                select(Lead).options(joinedload(Lead.assigned_user))
                .where(Lead.email == clean_email)
            ).first()
            if dup:
                owner = dup.assigned_user.name if dup.assigned_user else "unassigned"
                return RedirectResponse(
                    f"/new-lead?error=A+lead+with+email+{clean_email}+already+exists+—+assigned+to+{owner}.+<a+href='/leads/{dup.id}'>View+lead</a>",
                    status_code=303,
                )
        if clean_li:
            dup = s.scalars(
                select(Lead).options(joinedload(Lead.assigned_user))
                .where(Lead.linkedin_url == clean_li)
            ).first()
            if dup:
                owner = dup.assigned_user.name if dup.assigned_user else "unassigned"
                return RedirectResponse(
                    f"/new-lead?error=A+lead+with+this+LinkedIn+URL+already+exists+—+assigned+to+{owner}.+<a+href='/leads/{dup.id}'>View+lead</a>",
                    status_code=303,
                )

        lead = Lead(
            first_name=first_name.strip(),
            last_name=last_name.strip() or None,
            email=clean_email,
            domain=domain.strip().lower() or None,
            company=company.strip() or None,
            linkedin_url=clean_li,
            role_title=role_title.strip() or None,
            phone=phone.strip() or None,
            country=country.strip() or None,
            assigned_to=int(assigned_to) if assigned_to else user.id,
            status=LeadStatus.new,
            notes=notes.strip() or None,
        )
        s.add(lead)
        s.flush()
        _log_activity(s, lead.id, user.id, "created", f"Lead created by {user.name}")
        lead_id = lead.id
    return RedirectResponse(f"/leads/{lead_id}", status_code=303)


@app.post("/leads/{lead_id}/update-email")
def lead_update_email(request: Request, lead_id: int, email: str = Form("")):
    user = _require_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    clean_email = email.strip().lower()
    if not clean_email:
        return RedirectResponse(f"/leads/{lead_id}", status_code=303)
    with db.session() as s:
        lead = s.get(Lead, lead_id)
        if not lead:
            return RedirectResponse("/leads", status_code=302)
        old_email = lead.email
        lead.email = clean_email
        _log_activity(s, lead.id, user.id, "email_updated",
                      f"Email changed from {old_email or '(none)'} to {clean_email}")
    return RedirectResponse(f"/leads/{lead_id}", status_code=303)


@app.post("/leads/{lead_id}", response_class=HTMLResponse)
def lead_update(request: Request, lead_id: int, first_name: str = Form(""),
                last_name: str = Form(""), email: str = Form(""), domain: str = Form(""),
                company: str = Form(""), linkedin_url: str = Form(""), role_title: str = Form(""),
                phone: str = Form(""), country: str = Form(""),
                assigned_to: str = Form(""), status: str = Form("new"), notes: str = Form("")):
    user = _require_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)

    with db.session() as s:
        lead = s.get(Lead, lead_id)
        if not lead:
            return RedirectResponse("/leads", status_code=302)
        old_status = lead.status.value
        lead.first_name = first_name.strip()
        lead.last_name = last_name.strip() or None
        lead.email = email.strip() or None
        lead.domain = domain.strip().lower() or None
        lead.company = company.strip() or None
        lead.linkedin_url = linkedin_url.strip() or None
        lead.role_title = role_title.strip() or None
        lead.phone = phone.strip() or None
        lead.country = country.strip() or None
        lead.assigned_to = int(assigned_to) if assigned_to else None
        lead.status = LeadStatus(status)
        lead.notes = notes.strip() or None
        lead.last_activity_at = _now()
        if status != old_status:
            _log_activity(s, lead_id, user.id, "status_changed", f"{old_status} → {status}")
    return RedirectResponse(f"/leads/{lead_id}", status_code=303)


@app.post("/leads/{lead_id}/status")
def lead_status_change(request: Request, lead_id: int, status: str = Form("")):
    user = _require_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    with db.session() as s:
        lead = s.get(Lead, lead_id)
        if lead and status:
            old = lead.status.value
            lead.status = LeadStatus(status)
            lead.last_activity_at = _now()
            _log_activity(s, lead_id, user.id, "status_changed", f"{old} → {status}")
    return RedirectResponse("/", status_code=303)


@app.post("/leads/{lead_id}/delete")
def lead_delete(request: Request, lead_id: int):
    user = _require_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not _is_admin(user) and user.role != UserRole.manager.value:
        return RedirectResponse(f"/leads/{lead_id}", status_code=303)
    with db.session() as s:
        lead = s.get(Lead, lead_id)
        if lead:
            s.execute(select(LeadActivity).where(LeadActivity.lead_id == lead_id))
            for a in s.scalars(select(LeadActivity).where(LeadActivity.lead_id == lead_id)):
                s.delete(a)
            ls = s.scalars(select(LeadSequence).where(LeadSequence.lead_id == lead_id)).first()
            if ls:
                for em in s.scalars(select(SequenceEmail).where(SequenceEmail.lead_sequence_id == ls.id)):
                    s.delete(em)
                s.delete(ls)
            s.delete(lead)
    return RedirectResponse("/leads", status_code=303)


# --- Sequence assignment ---

@app.post("/leads/{lead_id}/sequence/assign")
def lead_assign_sequence(request: Request, lead_id: int, sequence_id: int = Form(0)):
    user = _require_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not sequence_id:
        return RedirectResponse(f"/leads/{lead_id}", status_code=303)

    with db.session() as s:
        lead = s.get(Lead, lead_id)
        seq = s.get(Sequence, sequence_id)
        if not lead or not seq:
            return RedirectResponse(f"/leads/{lead_id}", status_code=303)

        existing = s.scalars(select(LeadSequence).where(LeadSequence.lead_id == lead_id)).first()
        if existing:
            for em in s.scalars(select(SequenceEmail).where(SequenceEmail.lead_sequence_id == existing.id)):
                s.delete(em)
            s.delete(existing)
            s.flush()

        ls = LeadSequence(lead_id=lead_id, sequence_id=sequence_id, current_step=1)
        s.add(ls)
        s.flush()

        steps = s.scalars(
            select(SequenceStep).where(SequenceStep.sequence_id == sequence_id)
            .order_by(SequenceStep.step_number)
        ).all()

        for step in steps:
            subject = _render_vars(step.subject_template, lead)
            body = _render_vars(step.body_template, lead)
            s.add(SequenceEmail(
                lead_sequence_id=ls.id, step_id=step.id,
                subject=subject, body=body,
                status=SequenceEmailStatus.draft,
            ))

        _log_activity(s, lead_id, user.id, "sequence_assigned", f"Assigned sequence: {seq.name}")

    return RedirectResponse(f"/leads/{lead_id}", status_code=303)


# --- Email approve / skip ---

@app.post("/leads/{lead_id}/emails/{email_id}/approve")
def email_approve(request: Request, lead_id: int, email_id: int):
    user = _require_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    with db.session() as s:
        se = s.get(SequenceEmail, email_id)
        if not se:
            return RedirectResponse(f"/leads/{lead_id}", status_code=303)
        se.status = SequenceEmailStatus.approved
        se.sent_at = _now()

        ls = s.get(LeadSequence, se.lead_sequence_id)
        lead = s.get(Lead, lead_id)
        step = s.get(SequenceStep, se.step_id)
        if ls and lead and step:
            if lead.status == LeadStatus.new:
                lead.status = LeadStatus.contacted
                lead.last_activity_at = _now()
            elif lead.status == LeadStatus.contacted:
                lead.status = LeadStatus.following_up
                lead.last_activity_at = _now()
            ls.current_step = step.step_number + 1

        _log_activity(s, lead_id, user.id, "email_approved",
                      f"Approved email: step {step.step_number}" if step else "Approved email")

    return RedirectResponse(f"/leads/{lead_id}", status_code=303)


@app.post("/leads/{lead_id}/emails/{email_id}/skip")
def email_skip(request: Request, lead_id: int, email_id: int):
    user = _require_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    with db.session() as s:
        se = s.get(SequenceEmail, email_id)
        if not se:
            return RedirectResponse(f"/leads/{lead_id}", status_code=303)
        se.status = SequenceEmailStatus.skipped
        ls = s.get(LeadSequence, se.lead_sequence_id)
        step = s.get(SequenceStep, se.step_id)
        if ls and step:
            ls.current_step = step.step_number + 1
        _log_activity(s, lead_id, user.id, "email_skipped",
                      f"Skipped email: step {step.step_number}" if step else "Skipped email")

    return RedirectResponse(f"/leads/{lead_id}", status_code=303)


# ===================================================================
#  NEW LEAD — LinkedIn lookup + generate + create
# ===================================================================

@app.get("/new-lead", response_class=HTMLResponse)
def new_lead_page(request: Request, error: str = ""):
    user = _require_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    with db.session() as s:
        users = s.scalars(select(CRMUser).where(CRMUser.active.is_(True)).order_by(CRMUser.name)).all()
    return _page(request, "new_lead.html", users=users, error=error)


@app.get("/new-lead/linkedin-lookup", response_class=JSONResponse)
def linkedin_lookup_endpoint(url: str = Query("")):
    url = url.strip()
    if not url or "/in/" not in url:
        return JSONResponse({"error": "Not a LinkedIn profile URL"}, status_code=400)
    if not url.startswith("http"):
        url = "https://" + url
    return _linkedin_lookup(url)


@app.post("/new-lead", response_class=HTMLResponse)
def new_lead_generate(request: Request, first: str = Form(""), last: str = Form(""),
                      domain: str = Form(""), company: str = Form(""), role: str = Form(""),
                      phone: str = Form(""), country: str = Form(""),
                      linkedin_url: str = Form(""), verify_now: str = Form("")):
    user = _require_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)

    if linkedin_url.strip():
        li_url = linkedin_url.strip()
        if not li_url.startswith("http"):
            li_url = "https://" + li_url
        if not first.strip() or not domain.strip():
            info = _linkedin_lookup(li_url)
            first = first or info.get("first", "")
            last = last or info.get("last", "")
            company = company or info.get("company", "")
            role = role or info.get("role", "")
            if not domain.strip():
                domain = info.get("domain", "")

    with db.session() as s:
        sequences = s.scalars(select(Sequence).order_by(Sequence.name)).all()
        users = s.scalars(select(CRMUser).where(CRMUser.active.is_(True)).order_by(CRMUser.name)).all()
        existing_leads = s.scalars(
            select(Lead).options(joinedload(Lead.assigned_user))
            .order_by(Lead.first_name).limit(500)
        ).unique().all()

    base_ctx = dict(first=first, last=last, domain=domain, company=company, role=role,
                    phone=phone, country=country,
                    linkedin_url=linkedin_url, has_smtp_verify=HAS_EMAIL_VERIFIER,
                    sequences=sequences, users=users, existing_leads=existing_leads)

    if not first.strip():
        return _page(request, "new_lead.html", step="input", result=None, live_verify={},
                     pattern_hits=[], best_hit=None, ext_credit_info=None, dp=None,
                     error="Could not extract a name. Enter the name manually.", **base_ctx)

    if not domain.strip():
        return _page(request, "new_lead.html", step="input", result=None, live_verify={},
                     pattern_hits=[], best_hit=None, ext_credit_info=None, dp=None,
                     error="Could not detect the firm domain. Enter it manually.", **base_ctx)

    result, dp, pattern_hits, best_hit, live_verify, ext_credit_info = _run_generation(
        first, last, domain, linkedin_url, do_verify=bool(verify_now),
    )

    return _page(request, "new_lead.html", step="results", result=result, dp=dp,
                 live_verify=live_verify, pattern_hits=pattern_hits,
                 best_hit=best_hit, ext_credit_info=ext_credit_info, **base_ctx)


# ===================================================================
#  BULK IMPORT
# ===================================================================

_IMPORT_FIELD_ALIASES: dict[str, list[str]] = {
    "first_name": ["first name", "first", "fname", "given name", "first_name"],
    "last_name": ["last name", "last", "lname", "surname", "family name", "last_name"],
    "email": ["email", "mail", "email address", "e-mail", "work email"],
    "domain": ["domain", "website", "company domain"],
    "company": ["company", "company name", "organization", "org"],
    "linkedin_url": ["linkedin", "linkedin url", "linkedin profile", "linkedin_url"],
    "role_title": ["role", "title", "job title", "role title", "position", "role_title"],
    "phone": ["phone", "phone number", "telephone", "mobile"],
    "country": ["country", "location", "region"],
    "status": ["status", "lead status"],
    "notes": ["notes", "note", "comments", "description"],
}

_IMPORT_FIELDS = list(_IMPORT_FIELD_ALIASES.keys())


def _parse_upload(content: bytes, filename: str) -> tuple[list[str], list[dict]]:
    if filename.lower().endswith(".csv"):
        text = content.decode("utf-8-sig", errors="replace")
        reader = csv.DictReader(io.StringIO(text))
        headers = reader.fieldnames or []
        rows = list(reader)
    else:
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
        ws = wb.active
        all_rows = list(ws.iter_rows(values_only=True))
        wb.close()
        if not all_rows:
            return [], []
        headers = [str(c).strip() if c else f"Column_{i}" for i, c in enumerate(all_rows[0])]
        rows = []
        for row in all_rows[1:]:
            rows.append({h: (str(v).strip() if v is not None else "") for h, v in zip(headers, row)})
    return headers, rows


def _auto_map(headers: list[str]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    header_lower = {h: h.lower().strip() for h in headers}
    for field, aliases in _IMPORT_FIELD_ALIASES.items():
        for h, hl in header_lower.items():
            if hl in aliases:
                mapping[field] = h
                break
    return mapping


@app.get("/import-leads", response_class=HTMLResponse)
def import_leads_page(request: Request):
    user = _require_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    return _page(request, "import_leads.html", step="upload")


@app.post("/import-leads/preview", response_class=HTMLResponse)
async def import_leads_preview(request: Request, file: UploadFile):
    user = _require_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)

    content = await file.read()
    if len(content) > 5 * 1024 * 1024:
        return _page(request, "import_leads.html", step="upload",
                     error="File too large (max 5 MB).")

    fname = file.filename or "upload.csv"
    try:
        headers, rows = _parse_upload(content, fname)
    except Exception:
        return _page(request, "import_leads.html", step="upload",
                     error="Could not parse file. Ensure it is a valid CSV or XLSX.")

    if not headers or not rows:
        return _page(request, "import_leads.html", step="upload",
                     error="File is empty or has no data rows.")

    suggested = _auto_map(headers)
    file_b64 = base64.b64encode(content).decode()
    preview_rows = rows[:5]

    return _page(request, "import_leads.html", step="preview",
                 headers=headers, preview_rows=preview_rows,
                 total_rows=len(rows), suggested=suggested,
                 import_fields=_IMPORT_FIELDS,
                 file_data=file_b64, filename=fname)


@app.post("/import-leads/confirm", response_class=HTMLResponse)
async def import_leads_confirm(request: Request):
    user = _require_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)

    form = await request.form()
    file_b64 = form.get("file_data", "")
    fname = form.get("filename", "upload.csv")

    try:
        content = base64.b64decode(file_b64)
        headers, rows = _parse_upload(content, fname)
    except Exception:
        return _page(request, "import_leads.html", step="upload",
                     error="Could not decode file data. Please re-upload.")

    mapping: dict[str, str | None] = {}
    for field in _IMPORT_FIELDS:
        val = form.get(f"mapping_{field}", "")
        mapping[field] = val if val else None

    imported = 0
    skipped_dup = 0
    skipped_invalid = 0
    errors: list[dict] = []

    with db.session() as s:
        existing_emails: set[str] = set()
        for e in s.scalars(select(Lead.email).where(Lead.email.isnot(None))):
            if e:
                existing_emails.add(e.lower().strip())

        for i, row in enumerate(rows, start=2):
            try:
                def _get(field: str) -> str:
                    col = mapping.get(field)
                    if not col:
                        return ""
                    return (row.get(col) or "").strip()

                first = _get("first_name")
                if not first:
                    skipped_invalid += 1
                    continue

                email = _get("email").lower().strip()
                if email and email in existing_emails:
                    skipped_dup += 1
                    continue

                status_raw = _get("status").lower().replace(" ", "_")
                try:
                    status = LeadStatus(status_raw) if status_raw else LeadStatus.new
                except ValueError:
                    status = LeadStatus.new

                lead = Lead(
                    first_name=first,
                    last_name=_get("last_name") or None,
                    email=email or None,
                    domain=_get("domain") or None,
                    company=_get("company") or None,
                    linkedin_url=_get("linkedin_url") or None,
                    role_title=_get("role_title") or None,
                    phone=_get("phone") or None,
                    country=_get("country") or None,
                    status=status,
                    notes=_get("notes") or None,
                    assigned_to=user.id,
                )
                s.add(lead)
                s.flush()
                _log_activity(s, lead.id, user.id, "created", "Imported from file")

                if email:
                    existing_emails.add(email)
                imported += 1
            except Exception as exc:
                errors.append({"row": i, "reason": str(exc)})

    return _page(request, "import_leads.html", step="results",
                 imported=imported, skipped_dup=skipped_dup,
                 skipped_invalid=skipped_invalid, errors=errors)


# ===================================================================
#  SCOPE — Find Email (standalone page)
# ===================================================================

def _scope_base_ctx(s):
    users = s.scalars(select(CRMUser).where(CRMUser.active.is_(True)).order_by(CRMUser.name)).all()
    existing_leads = s.scalars(
        select(Lead).options(joinedload(Lead.assigned_user))
        .order_by(Lead.first_name).limit(500)
    ).unique().all()
    return dict(users=users, existing_leads=existing_leads)


@app.get("/scope", response_class=HTMLResponse)
def scope_page(request: Request, error: str = ""):
    user = _require_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    with db.session() as s:
        ctx = _scope_base_ctx(s)
    return _page(request, "scope.html", step="input", result=None, first="", last="",
                 domain="", company="", role="", phone="", country="", linkedin_url="",
                 has_smtp_verify=HAS_EMAIL_VERIFIER, live_verify={},
                 pattern_hits=[], best_hit=None, ext_credit_info=None,
                 dp=None, error=error, **ctx)


@app.get("/scope/linkedin-lookup", response_class=JSONResponse)
def scope_linkedin_lookup(url: str = Query("")):
    return linkedin_lookup_endpoint(url)


@app.post("/scope", response_class=HTMLResponse)
def scope_generate(request: Request, first: str = Form(""), last: str = Form(""),
                   domain: str = Form(""), company: str = Form(""), role: str = Form(""),
                   phone: str = Form(""), country: str = Form(""),
                   linkedin_url: str = Form("")):
    user = _require_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)

    if linkedin_url.strip():
        li_url = linkedin_url.strip()
        if not li_url.startswith("http"):
            li_url = "https://" + li_url
        if not first.strip() or not domain.strip():
            info = _linkedin_lookup(li_url)
            first = first or info.get("first", "")
            last = last or info.get("last", "")
            company = company or info.get("company", "")
            role = role or info.get("role", "")
            if not domain.strip():
                domain = info.get("domain", "")

    with db.session() as s:
        ctx = _scope_base_ctx(s)

    base_ctx = dict(first=first, last=last, domain=domain, company=company, role=role,
                    phone=phone, country=country,
                    linkedin_url=linkedin_url, has_smtp_verify=HAS_EMAIL_VERIFIER, **ctx)

    if not first.strip():
        return _page(request, "scope.html", step="input", result=None, live_verify={},
                     pattern_hits=[], best_hit=None, ext_credit_info=None, dp=None,
                     error="Could not extract a name. Enter the name manually.", **base_ctx)

    if not domain.strip():
        return _page(request, "scope.html", step="input", result=None, live_verify={},
                     pattern_hits=[], best_hit=None, ext_credit_info=None, dp=None,
                     error="Could not detect the firm domain. Enter it manually.", **base_ctx)

    result, dp, pattern_hits, best_hit, live_verify, ext_credit_info = _run_generation(
        first, last, domain, linkedin_url, do_verify=True,
    )

    return _page(request, "scope.html", step="results", result=result, dp=dp,
                 live_verify=live_verify, pattern_hits=pattern_hits,
                 best_hit=best_hit, ext_credit_info=ext_credit_info, **base_ctx)


# ===================================================================
#  SEQUENCES
# ===================================================================

@app.get("/sequences", response_class=HTMLResponse)
def sequences_page(request: Request):
    user = _require_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    with db.session() as s:
        seqs = s.scalars(
            select(Sequence).options(joinedload(Sequence.steps), joinedload(Sequence.owner))
            .order_by(Sequence.created_at.desc())
        ).unique().all()
    return _page(request, "sequences.html", sequences=seqs)


@app.get("/sequences/new", response_class=HTMLResponse)
def sequence_new(request: Request):
    user = _require_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    return _page(request, "sequence_detail.html", seq=None)


@app.post("/sequences", response_class=HTMLResponse)
async def sequence_create(request: Request):
    user = _require_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)

    form = await request.form()
    name = form.get("name", "").strip()
    if not name:
        return _page(request, "sequence_detail.html", seq=None, error="Sequence name is required")

    with db.session() as s:
        seq = Sequence(name=name, user_id=user.id)
        s.add(seq)
        s.flush()

        i = 1
        while f"step_{i}_subject" in form:
            subj = form.get(f"step_{i}_subject", "").strip()
            body = form.get(f"step_{i}_body", "").strip()
            delay = int(form.get(f"step_{i}_delay", "0") or "0")
            if subj and body:
                s.add(SequenceStep(
                    sequence_id=seq.id, step_number=i,
                    delay_days=delay, subject_template=subj, body_template=body,
                ))
            i += 1

        seq_id = seq.id
    return RedirectResponse(f"/sequences/{seq_id}", status_code=303)


@app.get("/sequences/{seq_id}", response_class=HTMLResponse)
def sequence_detail(request: Request, seq_id: int):
    user = _require_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    with db.session() as s:
        seq = s.scalars(
            select(Sequence).options(joinedload(Sequence.steps))
            .where(Sequence.id == seq_id)
        ).unique().first()
        if not seq:
            return RedirectResponse("/sequences", status_code=302)
    return _page(request, "sequence_detail.html", seq=seq)


@app.post("/sequences/{seq_id}", response_class=HTMLResponse)
async def sequence_update(request: Request, seq_id: int):
    user = _require_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)

    form = await request.form()
    name = form.get("name", "").strip()

    with db.session() as s:
        seq = s.get(Sequence, seq_id)
        if not seq:
            return RedirectResponse("/sequences", status_code=302)

        seq.name = name or seq.name
        for old_step in s.scalars(select(SequenceStep).where(SequenceStep.sequence_id == seq_id)):
            s.delete(old_step)
        s.flush()

        i = 1
        while f"step_{i}_subject" in form:
            subj = form.get(f"step_{i}_subject", "").strip()
            body = form.get(f"step_{i}_body", "").strip()
            delay = int(form.get(f"step_{i}_delay", "0") or "0")
            if subj and body:
                s.add(SequenceStep(
                    sequence_id=seq_id, step_number=i,
                    delay_days=delay, subject_template=subj, body_template=body,
                ))
            i += 1

    return RedirectResponse(f"/sequences/{seq_id}", status_code=303)


@app.post("/sequences/{seq_id}/delete")
def sequence_delete(request: Request, seq_id: int):
    user = _require_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    with db.session() as s:
        for step in s.scalars(select(SequenceStep).where(SequenceStep.sequence_id == seq_id)):
            s.delete(step)
        seq = s.get(Sequence, seq_id)
        if seq:
            s.delete(seq)
    return RedirectResponse("/sequences", status_code=303)


# ===================================================================
#  SETTINGS
# ===================================================================

@app.get("/settings", response_class=HTMLResponse)
def settings_redirect():
    return RedirectResponse("/settings/team", status_code=302)


@app.get("/settings/team", response_class=HTMLResponse)
def settings_team(request: Request, error: str = "", msg: str = ""):
    user = _require_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not _is_admin(user):
        return _page(request, "settings_team.html", users=[], managers=[], roles=_USER_ROLES,
                     error="Only admins can manage the team.")
    with db.session() as s:
        users = s.scalars(
            select(CRMUser).options(joinedload(CRMUser.leads), joinedload(CRMUser.manager))
            .order_by(CRMUser.name)
        ).unique().all()
        managers = [u for u in users if u.role in (UserRole.admin.value, UserRole.manager.value) and u.active]
    return _page(request, "settings_team.html", users=users, managers=managers,
                 roles=_USER_ROLES, error=error, msg=msg)


@app.post("/settings/team")
def settings_team_create(request: Request, name: str = Form(""), email: str = Form(""),
                         role: str = Form("member"), password: str = Form(""),
                         manager_id: str = Form("")):
    user = _require_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not _is_admin(user):
        return RedirectResponse("/settings/team?error=Only+admins+can+add+members", status_code=303)
    if not name.strip() or not email.strip():
        return RedirectResponse("/settings/team?error=Name+and+email+are+required", status_code=303)
    if password and len(password) < 6:
        return RedirectResponse("/settings/team?error=Password+must+be+at+least+6+characters", status_code=303)
    role_val = role if role in _USER_ROLES else UserRole.member.value
    mgr_id = int(manager_id) if manager_id else None
    with db.session() as s:
        existing = s.scalars(select(CRMUser).where(CRMUser.email == email.strip().lower())).first()
        if existing:
            return RedirectResponse("/settings/team?error=Email+already+exists", status_code=303)
        new_user = CRMUser(
            name=name.strip(),
            email=email.strip().lower(),
            role=role_val,
            is_admin=(role_val == UserRole.admin.value),
            password_hash=hash_password(password) if password else None,
            manager_id=mgr_id,
        )
        s.add(new_user)
    return RedirectResponse("/settings/team?msg=Member+added", status_code=303)


@app.post("/settings/team/{user_id}/edit")
async def settings_team_edit(request: Request, user_id: int):
    user = _require_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not _is_admin(user):
        return RedirectResponse("/settings/team?error=Only+admins+can+edit+members", status_code=303)
    form = await request.form()
    new_role = form.get("role", "member")
    new_name = (form.get("name") or "").strip()
    new_password = (form.get("password") or "").strip()
    if new_password and len(new_password) < 6:
        return RedirectResponse("/settings/team?error=Password+must+be+at+least+6+characters", status_code=303)
    role_val = new_role if new_role in _USER_ROLES else UserRole.member.value
    mgr_raw = (form.get("manager_id") or "").strip()
    mgr_id = int(mgr_raw) if mgr_raw else None
    with db.session() as s:
        u = s.get(CRMUser, user_id)
        if not u:
            return RedirectResponse("/settings/team?error=User+not+found", status_code=303)
        if new_name:
            u.name = new_name
        u.role = role_val
        u.is_admin = (role_val == UserRole.admin.value)
        u.manager_id = mgr_id
        if new_password:
            u.password_hash = hash_password(new_password)
    return RedirectResponse("/settings/team?msg=Member+updated", status_code=303)


@app.post("/settings/team/{user_id}/toggle")
def settings_team_toggle(request: Request, user_id: int):
    user = _require_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not _is_admin(user):
        return RedirectResponse("/settings/team?error=Only+admins+can+change+status", status_code=303)
    with db.session() as s:
        u = s.get(CRMUser, user_id)
        if u:
            u.active = not u.active
    return RedirectResponse("/settings/team", status_code=303)


@app.get("/settings/templates", response_class=HTMLResponse)
def settings_templates(request: Request):
    user = _require_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not _is_admin(user) and user.role != UserRole.manager.value:
        return _page(request, "settings_templates.html", templates=[], users=[],
                     edit_template=None, error="Only admins and managers can manage templates.")
    with db.session() as s:
        templates = s.scalars(
            select(MailTemplate).options(joinedload(MailTemplate.owner)).order_by(MailTemplate.name)
        ).unique().all()
        users = s.scalars(select(CRMUser).where(CRMUser.active.is_(True)).order_by(CRMUser.name)).all()
    return _page(request, "settings_templates.html", templates=templates, users=users, edit_template=None)


@app.post("/settings/templates")
def settings_template_create(request: Request, name: str = Form(""), subject: str = Form(""),
                             body: str = Form(""), user_id: str = Form("")):
    user = _require_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    with db.session() as s:
        s.add(MailTemplate(name=name.strip(), subject=subject.strip(), body=body,
                           user_id=int(user_id) if user_id else None))
    return RedirectResponse("/settings/templates", status_code=303)


@app.get("/settings/templates/{tmpl_id}/edit", response_class=HTMLResponse)
def settings_template_edit(request: Request, tmpl_id: int):
    user = _require_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    with db.session() as s:
        t = s.get(MailTemplate, tmpl_id)
        if not t:
            return RedirectResponse("/settings/templates", status_code=302)
        templates = s.scalars(select(MailTemplate).order_by(MailTemplate.name)).all()
        users = s.scalars(select(CRMUser).where(CRMUser.active.is_(True)).order_by(CRMUser.name)).all()
    return _page(request, "settings_templates.html", templates=templates, users=users, edit_template=t)


@app.post("/settings/templates/{tmpl_id}")
def settings_template_update(request: Request, tmpl_id: int, name: str = Form(""),
                             subject: str = Form(""), body: str = Form(""), user_id: str = Form("")):
    user = _require_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    with db.session() as s:
        t = s.get(MailTemplate, tmpl_id)
        if t:
            t.name = name.strip()
            t.subject = subject.strip()
            t.body = body
            t.user_id = int(user_id) if user_id else None
    return RedirectResponse("/settings/templates", status_code=303)


@app.post("/settings/templates/{tmpl_id}/delete")
def settings_template_delete(request: Request, tmpl_id: int):
    user = _require_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    with db.session() as s:
        t = s.get(MailTemplate, tmpl_id)
        if t:
            s.delete(t)
    return RedirectResponse("/settings/templates", status_code=303)


@app.get("/settings/account", response_class=HTMLResponse)
def settings_account(request: Request, msg: str = ""):
    user = _require_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    return _page(request, "settings_account.html", msg=msg)


@app.post("/settings/account")
def settings_account_update(request: Request, current_password: str = Form(""),
                            new_password: str = Form(""), confirm: str = Form("")):
    user = _require_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not verify_password(current_password, user.password_hash or ""):
        return RedirectResponse("/settings/account?msg=Current+password+is+incorrect", status_code=303)
    if len(new_password) < 6:
        return RedirectResponse("/settings/account?msg=New+password+must+be+6%2B+chars", status_code=303)
    if new_password != confirm:
        return RedirectResponse("/settings/account?msg=Passwords+do+not+match", status_code=303)
    with db.session() as s:
        u = s.get(CRMUser, user.id)
        if u:
            u.password_hash = hash_password(new_password)
    return RedirectResponse("/settings/account?msg=Password+updated", status_code=303)


# ===================================================================
#  VERIFY (power-user, hidden from nav)
# ===================================================================

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
