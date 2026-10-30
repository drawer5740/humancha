
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

SESSION_TTL_SECONDS = int(os.getenv("CAPTCHA_TTL_SECONDS", "120"))
MAX_ATTEMPTS = 10

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
    message = f"{session_id}:{answer.strip()}".encode("utf-8")

    return hmac.new(
        _derive_key("answer"),
        message,
        hashlib.sha256
    ).hexdigest()


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


def _delete_expired_sessions() -> None:
    now = datetime.now(timezone.utc).isoformat()

    (
        get_db()
        .table("captcha_sessions")
        .delete()
        .lt("expires_at", now)
        .execute()
    )


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


def _create_challenge() -> Response:

    _delete_expired_sessions()

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


# ==========================================
# REQUEST MODELS
# ==========================================

class VerifyBody(BaseModel):
    session_id: UUID
    captcha_hash: str = Field(
        min_length=20,
        max_length=512
    )
    answer: str = Field(max_length=128)


class RefreshBody(BaseModel):
    session_id: UUID
    captcha_hash: str = Field(
        min_length=20,
        max_length=512
    )


# ==========================================
# ROUTES
# ==========================================

@app.get("/", include_in_schema=False)
def homepage():
    return FileResponse(INDEX_FILE)


@app.get("/api/captcha")
def create_captcha(request: Request) -> Response:
    _enforce_rate_limit(request, "create", 20)
    return _create_challenge()


@app.get("/health", include_in_schema=False)
def health() -> dict:
    return {"ok": True}


@app.post("/api/captcha/verify")
def verify_captcha(
    body: VerifyBody,
    request: Request
) -> dict:

    _enforce_rate_limit(request, "verify", 60)

    session_id = str(body.session_id)

    if not _check_challenge_hash(
        session_id,
        body.captcha_hash
    ):
        raise HTTPException(
            status_code=401,
            detail="Invalid or expired challenge token."
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


@app.post("/api/captcha/refresh")
def refresh_captcha(
    body: RefreshBody,
    request: Request
) -> Response:

    _enforce_rate_limit(request, "refresh", 10)

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

    # Remove old session.
    (
        get_db()
        .table("captcha_sessions")
        .delete()
        .eq("id", session_id)
        .execute()
    )

    # Create replacement challenge.
    return _create_challenge()


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

    get_db().table(
        "captcha_sessions"
    ).select("id").limit(1).execute()

    return {"ok": True}
