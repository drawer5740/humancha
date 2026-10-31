
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time

from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from uuid import UUID, uuid4

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field
from supabase import Client, create_client

from renderer import generate_codes, render_webp

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
INDEX_FILE = BASE_DIR / "index.html"

SESSION_TTL_SECONDS = int(
    os.getenv("CAPTCHA_TTL_SECONDS", "120")
)

app = FastAPI(
    title="Humancha CAPTCHA API",
    docs_url="/api/docs",
    redoc_url=None
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
    expose_headers=[
        "X-Captcha-Session",
        "X-Captcha-Hash",
        "X-Captcha-Expires"
    ]
)


# ==========================================
# DATABASE
# ==========================================

@lru_cache(maxsize=1)
def get_db() -> Client:
    url = os.getenv("SUPABASE_URL")
    service_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

    if not url or not service_key:
        raise HTTPException(
            status_code=500,
            detail="Server database environment variables are not configured."
        )

    return create_client(url, service_key)


# ==========================================
# SECRET MANAGEMENT
# ==========================================

def _master_secret() -> bytes:
    value = os.getenv("CAPTCHA_SECRET", "")

    if len(value) < 40:
        raise HTTPException(
            status_code=500,
            detail="CAPTCHA_SECRET must be a randomly generated secret of at least 40 characters."
        )

    return value.encode("utf-8")


def _derive_key(label: str) -> bytes:
    return hmac.new(
        _master_secret(),
        f"humancha:{label}:v1".encode("utf-8"),
        hashlib.sha256
    ).digest()


def _answer_hmac(session_id: str, answer: str) -> str:
    message = (
        f"{session_id}:{answer.strip()}".encode("utf-8")
    )

    return hmac.new(
        _derive_key("answer"),
        message,
        hashlib.sha256
    ).hexdigest()


# ==========================================
# CHALLENGE TOKEN
# ==========================================

def _make_challenge_hash(
    session_id: str,
    expires_epoch: int
) -> str:

    nonce = secrets.token_urlsafe(18)

    message = (
        f"{session_id}:{expires_epoch}:{nonce}"
    ).encode("utf-8")

    signature = hmac.new(
        _derive_key("challenge-token"),
        message,
        hashlib.sha256
    ).hexdigest()

    return f"{expires_epoch}.{nonce}.{signature}"


def _check_challenge_hash(
    session_id: str,
    challenge_hash: str,
    *,
    allow_expired: bool = False
) -> bool:

    try:
        expiry_text, nonce, supplied_signature = (
            challenge_hash.split(".", 2)
        )

        expires_epoch = int(expiry_text)

    except (ValueError, AttributeError):
        return False

    if not allow_expired and expires_epoch <= int(time.time()):
        return False

    message = (
        f"{session_id}:{expires_epoch}:{nonce}"
    ).encode("utf-8")

    expected = hmac.new(
        _derive_key("challenge-token"),
        message,
        hashlib.sha256
    ).hexdigest()

    return hmac.compare_digest(
        expected,
        supplied_signature
    )


# ==========================================
# RATE LIMITING
# ==========================================

def _client_ip(request: Request) -> str:

    forwarded = request.headers.get(
        "x-forwarded-for",
        ""
    )

    if forwarded:
        parts = [
            part.strip()
            for part in forwarded.split(",")
            if part.strip()
        ]

        if parts:
            return parts[-1][:100]

    if request.client:
        return request.client.host[:100]

    return "unknown"


def _enforce_rate_limit(
    request: Request,
    action: str,
    limit: int
) -> None:

    ip_fingerprint = hmac.new(
        _derive_key("rate-limit"),
        f"{action}:{_client_ip(request)}".encode("utf-8"),
        hashlib.sha256
    ).hexdigest()

    result = get_db().rpc(
        "consume_captcha_rate_limit",
        {
            "p_key_hash": ip_fingerprint,
            "p_limit": limit
        }
    ).execute().data

    if isinstance(result, list) and result:
        result = result[0]

    if not isinstance(result, dict) or not result.get(
        "allowed", False
    ):
        raise HTTPException(
            status_code=429,
            detail="Too many requests. Please try again shortly."
        )

def _get_public_site(request: Request) -> dict:

    site_key = request.headers.get(
        "x-humancha-site-key", ""
    ).strip()

    if not site_key or len(site_key) > 128:
        raise HTTPException(
            status_code=401,
            detail="Missing or invalid site key."
        )

    rows = (
        get_db()
        .table("captcha_sites")
        .select("id, allowed_origins")
        .eq("site_key", site_key)
        .eq("active", True)
        .limit(1)
        .execute()
        .data
    ) or []

    if not rows:
        raise HTTPException(
            status_code=401,
            detail="Invalid site key."
        )

    site = rows[0]

    origin = request.headers.get(
        "origin", ""
    ).strip().rstrip("/")

    if not origin or origin not in (
        site.get("allowed_origins") or []
    ):
        raise HTTPException(
            status_code=403,
            detail="Origin is not allowed for this site."
        )

    return site


def _get_server_site(request: Request) -> dict:

    site_key = request.headers.get(
        "x-humancha-site-key", ""
    ).strip()

    site_secret = request.headers.get(
        "x-humancha-secret", ""
    )

    if (
        not site_key
        or not site_secret
        or len(site_key) > 128
        or len(site_secret) > 256
    ):
        raise HTTPException(
            status_code=401,
            detail="Missing site credentials."
        )

    rows = (
        get_db()
        .table("captcha_sites")
        .select("id, secret_hash")
        .eq("site_key", site_key)
        .eq("active", True)
        .limit(1)
        .execute()
        .data
    ) or []

    if not rows:
        raise HTTPException(
            status_code=401,
            detail="Invalid site credentials."
        )

    site = rows[0]

    supplied_hash = hashlib.sha256(
        site_secret.encode("utf-8")
    ).hexdigest()

    if not hmac.compare_digest(
        str(site["secret_hash"]),
        supplied_hash
    ):
        raise HTTPException(
            status_code=401,
            detail="Invalid site credentials."
        )

    return site


def _get_demo_site(request: Request) -> dict:

    site_key = os.getenv("HUMANCHA_DEMO_SITE_KEY", "")

    expected_origin = os.getenv(
        "HUMANCHA_DEMO_ORIGIN",
        "https://humancha.l.cd"
    ).rstrip("/")

    origin = request.headers.get(
        "origin", ""
    ).strip().rstrip("/")

    if not site_key:
        raise HTTPException(
            status_code=500,
            detail="Demo site is not configured."
        )

    if origin != expected_origin:
        raise HTTPException(
            status_code=403,
            detail="Demo origin is not allowed."
        )

    rows = (
        get_db()
        .table("captcha_sites")
        .select("id, allowed_origins")
        .eq("site_key", site_key)
        .eq("active", True)
        .limit(1)
        .execute()
        .data
    ) or []

    if not rows or origin not in (
        rows[0].get("allowed_origins") or []
    ):
        raise HTTPException(
            status_code=403,
            detail="Demo site configuration is invalid."
        )

    return rows[0]
    
# ==========================================
# SESSION CLEANUP
# ==========================================

def _delete_expired_sessions() -> None:

    now = datetime.now(timezone.utc).isoformat()

    (
        get_db()
        .table("captcha_sessions")
        .delete()
        .lt("expires_at", now)
        .execute()
    )


# ==========================================
# IMAGE RESPONSE
# ==========================================

def _image_response(
    webp: bytes,
    session_id: str,
    challenge_hash: str,
    expires_at: datetime
) -> Response:

    return Response(
        content=webp,
        media_type="image/webp",
        headers={
            "Content-Disposition":
                'inline; filename="CAPTCHA.WEBP"',

            "Cache-Control":
                "no-store, no-cache, must-revalidate, max-age=0",

            "Pragma": "no-cache",

            "X-Captcha-Session": session_id,

            "X-Captcha-Hash": challenge_hash,

            "X-Captcha-Expires":
                str(int(expires_at.timestamp()))
        }
    )


# ==========================================
# CHALLENGE CREATION
# ==========================================

def _create_challenge(site_id: str) -> Response:

    session_id = str(uuid4())

    hidden, decoy = generate_codes()

    webp = render_webp(hidden, decoy)

    expires_at = (
        datetime.now(timezone.utc)
        + timedelta(seconds=SESSION_TTL_SECONDS)
    )

    expires_epoch = int(expires_at.timestamp())

    get_db().table("captcha_sessions").insert({
        "id": session_id,
        "site_id": site_id,
        "answer_hmac": _answer_hmac(
            session_id,
            hidden
        ),
        "attempts": 0,
        "expires_at": expires_at.isoformat()
    }).execute()

    challenge_hash = _make_challenge_hash(
        session_id,
        expires_epoch
    )

    return _image_response(
        webp,
        session_id,
        challenge_hash,
        expires_at
    )

def _verify_for_site(
    body: VerifyBody,
    site_id: str
) -> dict:

    session_id = str(body.session_id)

    if not _check_challenge_hash(
        session_id,
        body.captcha_hash
    ):
        raise HTTPException(
            status_code=401,
            detail="Invalid or expired challenge token."
        )

    existing = (
        get_db()
        .table("captcha_sessions")
        .select("id")
        .eq("id", session_id)
        .eq("site_id", site_id)
        .limit(1)
        .execute()
        .data
    ) or []

    if not existing:
        raise HTTPException(
            status_code=404,
            detail="CAPTCHA session not found for this site."
        )

    candidate_hmac = _answer_hmac(
        session_id,
        body.answer
    )

    result = get_db().rpc(
        "verify_captcha",
        {
            "p_session_id": session_id,
            "p_answer_hmac": candidate_hmac
        }
    ).execute().data

    if isinstance(result, list) and result:
        result = result[0]

    if not isinstance(result, dict):
        raise HTTPException(
            status_code=500,
            detail="Unexpected verification response."
        )

    return result
# ==========================================
# REQUEST MODELS
# ==========================================

class VerifyBody(BaseModel):
    session_id: UUID

    captcha_hash: str = Field(
        min_length=20,
        max_length=512
    )

    answer: str = Field(
        max_length=128
    )


class RefreshBody(BaseModel):
    session_id: UUID

    captcha_hash: str = Field(
        min_length=20,
        max_length=512
    )


# ==========================================
# HOMEPAGE
# ==========================================

@app.get("/", include_in_schema=False)
def homepage():

    return FileResponse(INDEX_FILE)


@app.get("/humancha-logo.png", include_in_schema=False)
def humancha_logo():
    return FileResponse(
        BASE_DIR / "humancha-logo.png",
        media_type="image/png"
    )
@app.get("/terms/", include_in_schema=False)
def terms():
    return FileResponse(BASE_DIR / "terms" / "index.html")


@app.get("/privacy/", include_in_schema=False)
def privacy():
    return FileResponse(BASE_DIR / "privacy" / "index.html")

# ==========================================
# CAPTCHA CREATION
# ==========================================

@app.get("/api/captcha")
def create_captcha(request: Request) -> Response:

    _enforce_rate_limit(
        request,
        "create",
        20
    )

    return _create_challenge()


# ==========================================
# HEALTH CHECK
# ==========================================

@app.get("/health", include_in_schema=False)
def health() -> dict:

    return {
        "ok": True
    }


# ==========================================
# CAPTCHA VERIFICATION
# ==========================================

@app.post("/api/captcha/verify")
def verify_captcha(
    body: VerifyBody,
    request: Request
) -> dict:

    site = _get_server_site(request)

    _enforce_rate_limit(
        request,
        f"verify:{site['id']}",
        60
    )

    return _verify_for_site(
        body,
        str(site["id"])
    )

# shh....

@app.post("/api/demo/verify")
def verify_demo_captcha(
    body: VerifyBody,
    request: Request
) -> dict:

    site = _get_demo_site(request)

    _enforce_rate_limit(
        request,
        f"demo_verify:{site['id']}",
        60
    )

    return _verify_for_site(
        body,
        str(site["id"])
    )
# ==========================================
# CAPTCHA REFRESH
# ==========================================

@app.post("/api/captcha/refresh")
def refresh_captcha(
    body: RefreshBody,
    request: Request
) -> Response:

    _enforce_rate_limit(
        request,
        "refresh",
        10
    )

    session_id = str(body.session_id)

    if not _check_challenge_hash(
        session_id,
        body.captcha_hash,
        allow_expired=True
    ):
        raise HTTPException(
            status_code=401,
            detail="Invalid challenge token."
        )

    existing = (
        get_db()
        .table("captcha_sessions")
        .select("id")
        .eq("id", session_id)
        .limit(1)
        .execute()
        .data
    )

    if not existing:
        raise HTTPException(
            status_code=410,
            detail="Session no longer exists. Request a new CAPTCHA."
        )

    # Delete the previous session.
    (
        get_db()
        .table("captcha_sessions")
        .delete()
        .eq("id", session_id)
        .execute()
    )

    # Generate a replacement challenge.
    return _create_challenge()


# ==========================================
# SCHEDULED CLEANUP
# ==========================================

@app.get("/api/keepalive")
def keepalive(request: Request) -> dict:

    expected = os.getenv("CRON_SECRET", "")

    supplied = request.headers.get(
        "authorization",
        ""
    )

    if not expected or not hmac.compare_digest(
        supplied,
        f"Bearer {expected}"
    ):
        raise HTTPException(
            status_code=401,
            detail="Unauthorized"
        )

    _delete_expired_sessions()

    return {
        "ok": True
    }
