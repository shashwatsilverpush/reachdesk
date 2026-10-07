"""Exclusion lists.

Excluded observations are still *stored*, with an ``excluded_reason``; they
are needed for suppression and for counting. They simply never feed pattern
inference.
"""

from __future__ import annotations

import re

# --- Consumer mailbox domains ---------------------------------------------------
# Rationale: a personal mailbox says nothing about how a *company* names its
# addresses, and a gmail.com "domain pattern" would be meaningless. Includes
# regional variants and the major ISP mailboxes.
CONSUMER_DOMAINS = frozenset(
    {
        # Google
        "gmail.com", "googlemail.com",
        # Yahoo
        "yahoo.com", "yahoo.co.uk", "yahoo.co.in", "yahoo.in", "yahoo.fr", "yahoo.de",
        "yahoo.es", "yahoo.it", "yahoo.com.br", "yahoo.com.mx", "yahoo.com.ar",
        "yahoo.co.jp", "yahoo.com.au", "yahoo.ca", "yahoo.com.sg", "yahoo.com.ph",
        "yahoo.co.id", "ymail.com", "rocketmail.com",
        # Microsoft
        "outlook.com", "outlook.in", "outlook.fr", "outlook.de", "outlook.es", "outlook.it",
        "hotmail.com", "hotmail.co.uk", "hotmail.fr", "hotmail.de", "hotmail.es",
        "hotmail.it", "hotmail.com.br", "hotmail.com.ar", "live.com", "live.co.uk",
        "live.fr", "live.de", "live.in", "live.com.mx", "msn.com", "passport.com",
        # AOL / Verizon
        "aol.com", "aol.co.uk", "aim.com", "verizon.net",
        # Apple
        "icloud.com", "me.com", "mac.com",
        # Privacy-focused
        "proton.me", "protonmail.com", "protonmail.ch", "pm.me", "tutanota.com",
        "tuta.io", "fastmail.com", "fastmail.fm", "hey.com", "mailbox.org",
        "posteo.de", "hushmail.com",
        # Zoho personal
        "zoho.com", "zohomail.com", "zohomail.in",
        # Germany / Europe
        "gmx.com", "gmx.de", "gmx.net", "gmx.at", "gmx.ch", "web.de", "t-online.de",
        "freenet.de", "orange.fr", "wanadoo.fr", "free.fr", "laposte.net", "sfr.fr",
        "libero.it", "virgilio.it", "tiscali.it", "alice.it", "seznam.cz", "wp.pl",
        "o2.pl", "onet.pl", "interia.pl", "btinternet.com", "sky.com", "virginmedia.com",
        "ntlworld.com", "talktalk.net", "bluewin.ch", "telenet.be", "skynet.be",
        "ziggo.nl", "kpnmail.nl", "home.nl", "planet.nl",
        # Russia / CIS
        "yandex.ru", "yandex.com", "ya.ru", "mail.ru", "bk.ru", "inbox.ru", "list.ru",
        "rambler.ru",
        # East Asia
        "qq.com", "163.com", "126.com", "yeah.net", "sina.com", "sina.cn", "sohu.com",
        "aliyun.com", "foxmail.com", "naver.com", "daum.net", "hanmail.net",
        "nate.com", "hotmail.co.jp", "docomo.ne.jp", "ezweb.ne.jp",
        # South Asia
        "rediffmail.com", "rediff.com", "sify.com", "indiatimes.com",
        # Americas / ISPs
        "comcast.net", "att.net", "sbcglobal.net", "bellsouth.net", "cox.net",
        "charter.net", "earthlink.net", "optonline.net", "shaw.ca", "rogers.com",
        "sympatico.ca", "bol.com.br", "uol.com.br", "terra.com.br", "ig.com.br",
        # Misc global
        "mail.com", "email.com", "usa.com", "inbox.com", "lycos.com", "excite.com",
        "juno.com", "netzero.net", "bigpond.com", "optusnet.com.au",
    }
)


def is_consumer_domain(domain: str) -> bool:
    d = domain.lower().strip()
    if d in CONSUMER_DOMAINS:
        return True
    # Catch unlisted regional variants of the big providers (yahoo.co.th, hotmail.se...).
    head = d.split(".", 1)[0]
    return head in {"gmail", "yahoo", "hotmail", "outlook", "live", "gmx", "yandex", "aol"} \
        and d.count(".") <= 2


# --- Role addresses -------------------------------------------------------------
# Rationale: a role mailbox has no person attached, so it carries no naming
# convention. Matched after stripping separators and trailing digits, so
# sales1, ad-ops, ad_ops and ad.ops are all caught.
ROLE_LOCALS = frozenset(
    {
        "info", "sales", "support", "hello", "contact", "admin", "office", "team",
        "marketing", "press", "media", "careers", "jobs", "hr", "accounts", "billing",
        "noreply", "partnerships", "ads", "advertising", "adops", "publisher",
        "programmatic", "monetization", "monetisation", "webmaster",
        # Close relatives seen in ad-tech correspondence.
        "partners", "partner", "publishers", "advertise", "adsales", "business",
        "bizdev", "bd", "enquiries", "enquiry", "inquiries", "inquiry", "help",
        "mail", "email", "newsletter", "news", "editor", "editorial", "pr",
        "legal", "finance", "invoices", "invoice", "payments", "ap", "ar",
        "privacy", "compliance", "security", "abuse", "postmaster", "hostmaster",
        "it", "tech", "dev", "developers", "ops", "operations", "general",
        "mailer", "donotreply", "reply", "service", "customerservice", "feedback",
        "talent", "recruiting", "recruitment", "hiring", "affiliates", "affiliate",
        "revenue", "yield", "demand", "supply", "ssp", "dsp", "partnership",
        "growth", "social", "community", "events", "studio", "content", "video",
        "digital", "online", "web", "global", "hq", "reception", "hola", "contacto",
        "kontakt", "bonjour", "redaktion", "redazione", "redaccion", "info1",
    }
)
_ROLE_STRIP = re.compile(r"[.\-_]")
_DIGITS = re.compile(r"\d+$")


def is_role_address(local: str) -> bool:
    """True if a local part is a role mailbox (sales1, ad-ops, no-reply, ...)."""
    s = local.lower().split("+", 1)[0]
    s = _DIGITS.sub("", s)
    flat = _ROLE_STRIP.sub("", s)
    if flat in ROLE_LOCALS:
        return True
    # "sales.team", "ad-ops-uk": every separated piece is a role word or a region code.
    pieces = [p for p in _ROLE_STRIP.split(s) if p]
    return len(pieces) > 1 and all(p in ROLE_LOCALS or len(p) == 2 for p in pieces) \
        and any(p in ROLE_LOCALS for p in pieces)


# --- Placeholder first names ------------------------------------------------------
# Rationale: mail-merge fallbacks ("Hi there"). The address may be real, but the
# name tells us nothing, so the row cannot test a pattern.
PLACEHOLDER_NAMES = frozenset(
    {
        "there", "team", "hi", "hello", "hey", "folks", "sir", "madam", "publisher",
        "info", "sales", "admin", "na", "n/a", "", "nan", "none", "null", "friend",
        "all", "everyone", "partner", "partners", "colleague", "colleagues",
        "sir/madam", "sir or madam", "dear", "user", "customer", "contact", "unknown",
        "-", ".", "x", "test",
    }
)


def is_placeholder_name(name: object) -> bool:
    if name is None:
        return True
    s = str(name).strip().lower().strip(",.!")
    if s in PLACEHOLDER_NAMES:
        return True
    # Pure digits (a year or ID pasted into the name column) or no letters at all.
    return not re.search(r"[^\W\d_]", s)


# Order matters: the first applicable reason is recorded.
REASONS = ("invalid_email", "consumer_domain", "role_address", "placeholder_name")


def exclusion_reason(email: str | None, local: str | None, domain: str | None,
                     first_name: object) -> str | None:
    if not email:
        return "invalid_email"
    if is_consumer_domain(domain or ""):
        return "consumer_domain"
    if is_role_address(local or ""):
        return "role_address"
    if is_placeholder_name(first_name):
        return "placeholder_name"
    return None
