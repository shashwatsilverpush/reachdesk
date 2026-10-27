"""Authentication: password hashing, session cookies, OIDC SSO, FastAPI dependencies."""

from __future__ import annotations

import base64
import hashlib
import secrets
from urllib.parse import urlencode

import bcrypt as _bcrypt
import httpx
from fastapi import Request
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from leadcore import config, db
from leadcore.models import CRMUser

_serializer: URLSafeTimedSerializer | None = None


def _get_serializer() -> URLSafeTimedSerializer:
    global _serializer
    if _serializer is None:
        _serializer = URLSafeTimedSerializer(config.SECRET_KEY)
    return _serializer


def hash_password(plain: str) -> str:
    return _bcrypt.hashpw(plain.encode(), _bcrypt.gensalt()).decode()


def verify_password(plain: str, hashed: str) -> bool:
    return _bcrypt.checkpw(plain.encode(), hashed.encode())


def create_session_cookie(user_id: int) -> str:
    return _get_serializer().dumps({"uid": user_id})


def read_session_cookie(cookie: str) -> int | None:
    try:
        data = _get_serializer().loads(cookie, max_age=7 * 86400)
        return data.get("uid")
    except (BadSignature, SignatureExpired):
        return None


def get_current_user(request: Request) -> CRMUser | None:
    """Read session cookie and return the logged-in CRMUser, or None."""
    cookie = request.cookies.get("leadcore_session")
    if not cookie:
        return None
    uid = read_session_cookie(cookie)
    if uid is None:
        return None
    with db.session() as s:
        user = s.get(CRMUser, uid)
        if user and user.active:
            s.expunge(user)
            return user
    return None


# ---------------------------------------------------------------------------
# OIDC / PKCE helpers for auth.silverpush.live
# ---------------------------------------------------------------------------

SSO_ENABLED = bool(config.SSO_CLIENT_ID)

_AUTHORIZE_URL = f"{config.SSO_ISSUER}/oauth/authorize"
_TOKEN_URL = f"{config.SSO_ISSUER}/oauth/token"
_USERINFO_URL = f"{config.SSO_ISSUER}/oauth/userinfo"


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def generate_pkce() -> tuple[str, str]:
    verifier = _b64url(secrets.token_bytes(32))
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


def build_authorize_url() -> tuple[str, dict]:
    """Return (redirect_url, params_to_store_in_cookie)."""
    state = secrets.token_urlsafe(24)
    nonce = secrets.token_urlsafe(24)
    verifier, challenge = generate_pkce()

    params = {
        "response_type": "code",
        "client_id": config.SSO_CLIENT_ID,
        "redirect_uri": config.SSO_REDIRECT_URI,
        "scope": "openid email profile",
        "state": state,
        "nonce": nonce,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    url = f"{_AUTHORIZE_URL}?{urlencode(params)}"

    cookie_data = {"state": state, "nonce": nonce, "verifier": verifier}
    return url, cookie_data


def create_sso_state_cookie(data: dict) -> str:
    return _get_serializer().dumps(data)


def read_sso_state_cookie(cookie: str) -> dict | None:
    try:
        return _get_serializer().loads(cookie, max_age=300)
    except (BadSignature, SignatureExpired):
        return None


def exchange_code(code: str, verifier: str) -> tuple[dict | None, str]:
    """Exchange authorization code for tokens. Returns (token_dict, error_detail)."""
    payload = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": config.SSO_REDIRECT_URI,
        "code_verifier": verifier,
    }
    if config.SSO_CLIENT_SECRET:
        creds = base64.b64encode(
            f"{config.SSO_CLIENT_ID}:{config.SSO_CLIENT_SECRET}".encode()
        ).decode()
        headers = {"Authorization": f"Basic {creds}"}
    else:
        payload["client_id"] = config.SSO_CLIENT_ID
        headers = {}
    try:
        with httpx.Client(timeout=15) as client:
            resp = client.post(_TOKEN_URL, data=payload, headers=headers)
            if resp.status_code != 200:
                try:
                    body = resp.json()
                    detail = body.get("error_description") or body.get("error") or resp.text
                except Exception:
                    detail = resp.text
                return None, f"HTTP {resp.status_code}: {detail}"
            return resp.json(), ""
    except Exception as exc:
        return None, str(exc)


def fetch_userinfo(access_token: str) -> dict | None:
    """Fetch user profile from the OIDC userinfo endpoint."""
    try:
        with httpx.Client(timeout=10) as client:
            resp = client.get(
                _USERINFO_URL,
                headers={"Authorization": f"Bearer {access_token}"},
            )
            if resp.status_code != 200:
                return None
            return resp.json()
    except Exception:
        return None
