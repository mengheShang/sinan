"""Connect a Gmail account with read-only OAuth, and report token health.

Flow: the signed-in Dashboard asks /oauth/google/start for a consent URL and
sends the browser there. Google redirects back to /oauth/google/callback,
which is public because the Strict login cookie is not sent on that
cross-site redirect. The one-time state stands in for it (app/oauth.py).
"""

import logging
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from typing import Annotated, Literal
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.crypto import TokenCipher, generate_key
from app.db.accounts import connect_account, list_accounts, refresh_read_token
from app.db.session import get_engine
from app.oauth import PendingFlows, TokenStatus, expires_at, scopes_ok, token_status
from app.providers import factory
from app.providers.demo_data import DEMO_EMAIL
from app.providers.fakes import FakeOAuth
from app.providers.oauth import OAuthClient, OAuthError
from apps.api.auth import public

log = logging.getLogger("sinan.api.oauth")

Clock = Callable[[], datetime]


def utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass
class Connector:
    client: OAuthClient
    cipher: TokenCipher
    flows: PendingFlows
    dashboard_url: str
    clock: Clock = utc_now


def build_connector(settings: Settings) -> Connector:
    """Real Google client in live mode; a fake that skips Google in demo mode."""
    flows = PendingFlows()
    dashboard = settings.sinan_dashboard_url.rstrip("/")
    redirect = settings.sinan_oauth_redirect_uri
    if settings.sinan_mode == "demo":
        # Demo tokens are fake, so a per-process key is enough
        fake = FakeOAuth(DEMO_EMAIL, redirect)
        return Connector(fake, TokenCipher(generate_key()), flows, dashboard)
    google = factory.google_oauth(settings)
    cipher = factory.token_cipher(settings)
    return Connector(google, cipher, flows, dashboard)


@lru_cache
def get_connector() -> Connector:
    return build_connector(get_settings())


def get_session() -> Iterator[Session]:
    with Session(get_engine()) as session:
        yield session


ConnectorDep = Annotated[Connector, Depends(get_connector)]
SessionDep = Annotated[Session, Depends(get_session)]

router = APIRouter()


class StartBody(BaseModel):
    role: Literal["school", "personal"]


@router.post("/oauth/google/start")
def start(body: StartBody, conn: ConnectorDep) -> dict[str, str]:
    """Begin a connect flow. Requires the login session (app-wide check)."""
    flow = conn.flows.start(body.role, conn.clock())
    url = conn.client.authorize_url(flow.state, flow.challenge)
    return {"url": url}


def back_to_dashboard(conn: Connector, result: str) -> RedirectResponse:
    query = urlencode({"oauth": result})
    url = f"{conn.dashboard_url}/accounts?{query}"
    return RedirectResponse(url, status_code=status.HTTP_303_SEE_OTHER)


@router.get("/oauth/google/callback")
@public
def callback(
    conn: ConnectorDep,
    session: SessionDep,
    state: str = "",
    code: str | None = None,
    error: str | None = None,
) -> RedirectResponse:
    """Finish a connect flow. Every outcome redirects with a short result code."""
    flow = conn.flows.finish(state, conn.clock())
    if flow is None:
        return back_to_dashboard(conn, "expired")
    if error is not None:
        result = "denied" if error == "access_denied" else "error"
        return back_to_dashboard(conn, result)
    if not code:
        return back_to_dashboard(conn, "error")
    try:
        grant = conn.client.exchange(code, flow.verifier)
        if not scopes_ok(grant.scopes):
            # Missing gmail.readonly, or anything beyond it: store nothing
            return back_to_dashboard(conn, "scope")
        email = conn.client.mailbox_email(grant.access_token)
    except OAuthError as exc:
        log.warning("OAuth callback failed: %s", exc)
        return back_to_dashboard(conn, "error")
    scopes = " ".join(sorted(grant.scopes))
    now = conn.clock()
    connect_account(session, conn.cipher, email, flow.role, grant.refresh_token, scopes, now)
    session.commit()
    log.info("Account connected with read-only access")
    return back_to_dashboard(conn, "connected")


class AccountOut(BaseModel):
    id: uuid.UUID
    email: str
    role: str
    authorized_at: datetime | None
    expires_at: datetime | None
    status: TokenStatus
    last_error: str | None
    last_sync_at: datetime | None


@router.get("/accounts")
def accounts(conn: ConnectorDep, session: SessionDep) -> list[AccountOut]:
    now = conn.clock()
    out = []
    for view in list_accounts(session):
        state: TokenStatus
        if view.authorized_at is None:
            ends = None
            state = "needs_reauth"
        else:
            ends = expires_at(view.authorized_at)
            state = token_status(view.authorized_at, view.needs_reauth, now)
        item = AccountOut(
            id=view.id,
            email=view.email,
            role=view.role,
            authorized_at=view.authorized_at,
            expires_at=ends,
            status=state,
            last_error=view.last_error,
            last_sync_at=view.last_sync_at,
        )
        out.append(item)
    return out


@router.post("/accounts/{account_id}/check")
def check(account_id: uuid.UUID, conn: ConnectorDep, session: SessionDep) -> dict[str, str]:
    """Try the read token now. An expired or revoked one flags the account."""
    known = {v.id for v in list_accounts(session)}
    if account_id not in known:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown account")
    try:
        token = refresh_read_token(session, conn.cipher, conn.client, account_id)
    except OAuthError as exc:
        log.warning("Token check failed: %s", exc)
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Provider error") from exc
    session.commit()
    result = "ok" if token is not None else "needs_reauth"
    return {"status": result}
