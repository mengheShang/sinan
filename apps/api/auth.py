"""Login routes and the session check applied to every other API route."""

import secrets
import threading
from collections.abc import Callable
from functools import lru_cache
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from app.auth import (
    MIN_SECRET_LENGTH,
    Auth,
    LoginLimiter,
    SessionSigner,
    hash_password,
    parse_hash,
    verify_password,
)
from app.config import Settings, get_settings

COOKIE = "sinan_session"
# Documented in .env.example and the README. Only used in demo mode.
DEMO_PASSWORD = "sinan-demo"  # noqa: S105 - public by design, demo mode only

# Endpoints reachable without a session, marked with @public.
# Everything else requires one, so a new route is protected by default.
PUBLIC_ENDPOINTS: set[Callable[..., Any]] = set()

# One login at a time, so parallel guesses cannot slip past the limiter
login_lock = threading.Lock()


def build_auth(settings: Settings) -> Auth:
    """Build login state from settings. Refuses a live setup without both values."""
    hash_value = settings.sinan_dashboard_password_hash
    secret_value = settings.sinan_session_secret
    live = settings.sinan_mode == "live"
    if live and hash_value is None:
        raise ValueError("SINAN_DASHBOARD_PASSWORD_HASH is required in live mode")
    if live and secret_value is None:
        raise ValueError("SINAN_SESSION_SECRET is required in live mode")
    if hash_value is None:
        password_hash = hash_password(DEMO_PASSWORD)
    else:
        password_hash = hash_value.get_secret_value()
        parse_hash(password_hash)
    # Demo only: without a configured secret, sessions end when the API restarts
    random_secret = secrets.token_urlsafe(32)
    secret = random_secret if secret_value is None else secret_value.get_secret_value()
    if len(secret) < MIN_SECRET_LENGTH:
        raise ValueError("SINAN_SESSION_SECRET must be at least 32 characters")
    signer = SessionSigner(secret, password_hash)
    return Auth(password_hash, signer, LoginLimiter())


@lru_cache
def get_auth() -> Auth:
    return build_auth(get_settings())


AuthDep = Annotated[Auth, Depends(get_auth)]


def require_session(request: Request, auth: AuthDep) -> None:
    """App-wide dependency: a valid session cookie, unless the route is public."""
    route = request.scope.get("route")
    endpoint = getattr(route, "endpoint", None)
    if endpoint in PUBLIC_ENDPOINTS:
        return
    token = request.cookies.get(COOKIE)
    if token is None or not auth.signer.check(token, auth.clock()):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not signed in")


def public[F: Callable[..., Any]](endpoint: F) -> F:
    """Mark an endpoint as reachable without a session."""
    PUBLIC_ENDPOINTS.add(endpoint)
    return endpoint


class LoginBody(BaseModel):
    password: str = Field(max_length=1024)


router = APIRouter(prefix="/auth")


@router.post("/login")
@public
def login(body: LoginBody, response: Response, auth: AuthDep) -> dict[str, str]:
    with login_lock:
        now = auth.clock()
        wait = auth.limiter.retry_after(now)
        if wait:
            headers = {"Retry-After": str(wait)}
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many attempts", headers)
        if not verify_password(body.password, auth.password_hash):
            auth.limiter.fail(now)
            # Same answer for every failure; no hint about why
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Login failed")
        auth.limiter.reset()
        token = auth.signer.issue(now)
    # Plain HTTP on 127.0.0.1, so the cookie cannot be marked Secure
    response.set_cookie(
        COOKIE,
        token,
        max_age=auth.signer.ttl,
        httponly=True,
        samesite="strict",
        path="/",
    )
    return {"status": "ok"}


@router.post("/logout")
@public
def logout(response: Response) -> dict[str, str]:
    response.delete_cookie(COOKIE, httponly=True, samesite="strict", path="/")
    return {"status": "ok"}


@router.get("/session")
def session() -> dict[str, str]:
    """Reports whether the caller is signed in. The app-wide check does the work."""
    return {"status": "ok"}
