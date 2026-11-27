"""Connect Google accounts with OAuth, and report token health.

Three flows share one callback (ADR 0003: one fixed redirect URI):

- read: /oauth/google/start connects a mailbox with `gmail.readonly`.
- write ("Enable actions") and calendar ("Connect calendar"):
  /accounts/{id}/oauth/{purpose} starts a flow for one connected account,
  with that address as the login hint (ADR 0021, decision 2). The callback
  stores the token only when Google granted exactly the flow's scopes and
  the authorized address is that account's. The token is sealed with the
  Executor's public key; the API cannot open it again.

The signed-in Dashboard asks for a consent URL and sends the browser there.
Google redirects back to /oauth/google/callback, which is public because the
Strict login cookie is not sent on that cross-site redirect. The one-time
state stands in for it, and says which flow it was (app/oauth.py).

Needs the owner's line-by-line review (OAuth, read and write permissions).
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
from app.db.credentials import Health, save_sealed, sealed_health
from app.db.models import Account
from app.db.session import get_engine
from app.oauth import (
    REQUESTED,
    PendingFlow,
    PendingFlows,
    TokenStatus,
    expires_at,
    scopes_ok,
    token_status,
)
from app.providers import factory
from app.providers.demo_data import DEMO_EMAIL
from app.providers.fakes import FakeOAuth
from app.providers.oauth import OAuthClient, OAuthError, TokenGrant
from app.sealing import Sealer, generate_pair
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
    # Seals write and calendar tokens for the Executor; None in live mode
    # until SINAN_EXECUTOR_PUBLIC_KEY is set, which keeps those flows off
    sealer: Sealer | None = None


def build_connector(settings: Settings) -> Connector:
    """Real Google client in live mode; a fake that skips Google in demo mode."""
    flows = PendingFlows()
    dashboard = settings.sinan_dashboard_url.rstrip("/")
    redirect = settings.sinan_oauth_redirect_uri
    if settings.sinan_mode == "demo":
        # Demo tokens are fake, so a per-process key is enough
        fake = FakeOAuth(DEMO_EMAIL, redirect)
        # Demo tokens are fake; a per-process pair lets the flows run
        _, public_key = generate_pair()
        cipher = TokenCipher(generate_key())
        return Connector(fake, cipher, flows, dashboard, sealer=Sealer(public_key))
    google = factory.google_oauth(settings)
    cipher = factory.token_cipher(settings)
    key = settings.sinan_executor_public_key
    sealer = Sealer(key) if key is not None else None
    return Connector(google, cipher, flows, dashboard, sealer=sealer)


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
    url = conn.client.authorize_url(flow.state, flow.challenge, REQUESTED["read"])
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
    except OAuthError as exc:
        log.warning("OAuth callback failed: %s", exc)
        return back_to_dashboard(conn, "error")
    if not scopes_ok(grant.scopes, flow.purpose):
        # A scope missing, or anything beyond the flow's: store nothing
        return back_to_dashboard(conn, "scope")
    if flow.purpose != "read":
        return finish_sealed(conn, session, flow, grant)
    try:
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


def authorized_email(conn: Connector, flow: PendingFlow, grant: TokenGrant) -> str:
    """Which account granted the token: the Gmail profile for a write token,
    the verified OpenID address for a calendar token. Raises OAuthError."""
    if flow.purpose == "write":
        return conn.client.mailbox_email(grant.access_token)
    return conn.client.account_email(grant.access_token)


def finish_sealed(
    conn: Connector, session: Session, flow: PendingFlow, grant: TokenGrant
) -> RedirectResponse:
    """Store a write or calendar token, sealed for the Executor, only for the
    account the flow was started from."""
    account = session.get(Account, flow.account_id) if flow.account_id else None
    if account is None or conn.sealer is None:
        return back_to_dashboard(conn, "error")
    try:
        email = authorized_email(conn, flow, grant)
    except OAuthError as exc:
        log.warning("OAuth callback failed: %s", exc)
        return back_to_dashboard(conn, "error")
    if email.casefold() != account.email.casefold():
        # Another Google account was chosen on the consent screen
        return back_to_dashboard(conn, "wrong_account")
    scopes = " ".join(sorted(grant.scopes))
    now = conn.clock()
    save_sealed(session, conn.sealer, account.id, flow.purpose, grant.refresh_token, scopes, now)
    session.commit()
    log.info("Sealed a %s token for the Executor", flow.purpose)
    result = "actions_enabled" if flow.purpose == "write" else "calendar_connected"
    return back_to_dashboard(conn, result)


SealedPurpose = Literal["write", "calendar"]


@router.post("/accounts/{account_id}/oauth/{purpose}")
def start_sealed(
    account_id: uuid.UUID, purpose: SealedPurpose, conn: ConnectorDep, session: SessionDep
) -> dict[str, str]:
    """Begin "Enable actions" (write) or "Connect calendar" for one account.

    409 while SINAN_EXECUTOR_PUBLIC_KEY is not set: there is nothing to seal
    the token with, so it would not be stored.
    """
    account = session.get(Account, account_id)
    if account is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown account")
    if conn.sealer is None:
        detail = "Actions are off until SINAN_EXECUTOR_PUBLIC_KEY is set"
        raise HTTPException(status.HTTP_409_CONFLICT, detail)
    flow = conn.flows.start(account.role, conn.clock(), purpose, account.id)
    scopes = REQUESTED[purpose]
    url = conn.client.authorize_url(flow.state, flow.challenge, scopes, account.email)
    return {"url": url}


class GrantOut(BaseModel):
    """A write or calendar token's state; never the token."""

    authorized_at: datetime
    expires_at: datetime
    status: TokenStatus
    last_error: str | None


class AccountOut(BaseModel):
    id: uuid.UUID
    email: str
    role: str
    authorized_at: datetime | None
    expires_at: datetime | None
    status: TokenStatus
    last_error: str | None
    last_sync_at: datetime | None
    # ADR 0021: None until the owner enables actions or connects the calendar
    write: GrantOut | None = None
    calendar: GrantOut | None = None
    # Whether those flows can run: the Executor's public key is set
    can_enable: bool = False


def grant_out(health: Health | None, now: datetime) -> GrantOut | None:
    if health is None:
        return None
    state = token_status(health.authorized_at, health.needs_reauth, now)
    ends = expires_at(health.authorized_at)
    return GrantOut(
        authorized_at=health.authorized_at,
        expires_at=ends,
        status=state,
        last_error=health.last_error,
    )


@router.get("/accounts")
def accounts(conn: ConnectorDep, session: SessionDep) -> list[AccountOut]:
    now = conn.clock()
    sealed = sealed_health(session)
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
            write=grant_out(sealed.get((view.id, "write")), now),
            calendar=grant_out(sealed.get((view.id, "calendar")), now),
            can_enable=conn.sealer is not None,
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
