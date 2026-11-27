"""The "Enable actions" and "Connect calendar" flows (ADR 0021, decisions 1 and 2).

A fake Google and a throwaway database. Every address, key and token is
fictional or generated for the test. The API seals the token; only the
matching private key, which the API never holds, opens it.
"""

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session

from app.auth import Auth, LoginLimiter, SessionSigner, hash_password
from app.crypto import DecryptError, TokenCipher, generate_key
from app.db.credentials import load_credential
from app.db.models import Account, Credential
from app.oauth import GRANTED, REQUESTED, PendingFlows, scopes_ok
from app.providers.fakes import FakeOAuth
from app.providers.google_oauth import USERINFO_URL, GoogleOAuth
from app.providers.oauth import (
    CALENDAR_SCOPE,
    EMAIL_GRANTED,
    OPENID,
    READ_SCOPE,
    WRITE_SCOPE,
    OAuthError,
)
from app.sealing import Opener, Sealer, generate_pair
from apps.api.auth import COOKIE, get_auth
from apps.api.main import app
from apps.api.oauth import Connector, get_connector, get_session

EMAIL = "student@example.edu"
OTHER = "someone.else@example.com"
REDIRECT = "http://127.0.0.1:8000/oauth/google/callback"
DASHBOARD = "http://127.0.0.1:3000"
SECRET = "fake-session-secret-for-tests-only-0123456789"
NOW = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)


# ---- Scopes (pure) ----


def test_the_write_and_calendar_scopes() -> None:
    assert WRITE_SCOPE == "https://www.googleapis.com/auth/gmail.modify"
    assert CALENDAR_SCOPE == "https://www.googleapis.com/auth/calendar.events"
    assert REQUESTED["write"] == (WRITE_SCOPE,)
    assert REQUESTED["calendar"] == (CALENDAR_SCOPE, "openid", "email")


@pytest.mark.parametrize(
    ("purpose", "granted"),
    [
        ("write", frozenset()),
        ("write", frozenset({WRITE_SCOPE, READ_SCOPE})),
        ("write", frozenset({WRITE_SCOPE, "https://www.googleapis.com/auth/gmail.send"})),
        ("write", frozenset({READ_SCOPE})),
        ("calendar", frozenset({CALENDAR_SCOPE})),
        ("calendar", frozenset({CALENDAR_SCOPE, OPENID})),
        ("calendar", frozenset({OPENID, EMAIL_GRANTED})),
        ("calendar", frozenset({CALENDAR_SCOPE, OPENID, EMAIL_GRANTED, WRITE_SCOPE})),
        ("read", frozenset({READ_SCOPE, WRITE_SCOPE})),
    ],
)
def test_only_the_exact_set_is_accepted(purpose: str, granted: frozenset[str]) -> None:
    assert not scopes_ok(granted, purpose)  # type: ignore[arg-type]


@pytest.mark.parametrize("purpose", ["read", "write", "calendar"])
def test_the_exact_set_is_accepted(purpose: str) -> None:
    assert scopes_ok(GRANTED[purpose], purpose)  # type: ignore[index, arg-type]


def test_a_sealed_flow_needs_its_account() -> None:
    flows = PendingFlows()
    with pytest.raises(ValueError):
        flows.start("personal", NOW, "write")
    with pytest.raises(ValueError):
        flows.start("personal", NOW, "read", uuid.uuid4())


def test_the_flow_remembers_its_purpose_and_account() -> None:
    flows = PendingFlows()
    account = uuid.uuid4()
    started = flows.start("personal", NOW, "calendar", account)
    found = flows.finish(started.state, NOW)
    assert found is not None
    assert (found.purpose, found.account_id) == ("calendar", account)


# ---- Google client (pure, mocked transport) ----


def google(handler: httpx.MockTransport) -> GoogleOAuth:
    return GoogleOAuth(
        "fake-client-id", SecretStr("fake-secret"), REDIRECT, httpx.Client(transport=handler)
    )


def test_authorize_url_asks_the_flow_scopes_with_a_hint() -> None:
    client = google(httpx.MockTransport(lambda r: httpx.Response(500)))
    url = client.authorize_url("s", "c", REQUESTED["calendar"], EMAIL)
    query = parse_qs(urlsplit(url).query)
    assert query["scope"] == [f"{CALENDAR_SCOPE} openid email"]
    assert query["login_hint"] == [EMAIL]


def test_authorize_url_without_hint_has_none() -> None:
    client = google(httpx.MockTransport(lambda r: httpx.Response(500)))
    query = parse_qs(urlsplit(client.authorize_url("s", "c")).query)
    assert "login_hint" not in query
    assert query["scope"] == [READ_SCOPE]


def test_account_email_needs_a_verified_address() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == USERINFO_URL
        return httpx.Response(200, json={"email": EMAIL, "email_verified": False})

    with pytest.raises(OAuthError):
        google(httpx.MockTransport(handler)).account_email(SecretStr("fake-access"))


def test_account_email_reads_userinfo() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"email": EMAIL, "email_verified": True})

    found = google(httpx.MockTransport(handler)).account_email(SecretStr("fake-access"))
    assert found == EMAIL


# ---- API (database) ----

db = pytest.mark.db


class Keys:
    def __init__(self) -> None:
        private, public = generate_pair()
        self.opener = Opener(private)
        self.sealer = Sealer(public)


@pytest.fixture
def keys() -> Keys:
    return Keys()


@pytest.fixture
def fake() -> FakeOAuth:
    return FakeOAuth(EMAIL, REDIRECT)


@pytest.fixture
def cipher() -> TokenCipher:
    return TokenCipher(generate_key())


@pytest.fixture
def session(conn: Connection) -> Session:
    return Session(bind=conn, join_transaction_mode="create_savepoint")


def make_client(
    session: Session, fake: FakeOAuth, cipher: TokenCipher, sealer: Sealer | None
) -> Iterator[TestClient]:
    connector = Connector(fake, cipher, PendingFlows(), DASHBOARD, lambda: NOW, sealer)
    password_hash = hash_password("unused", n=2**4)
    signer = SessionSigner(SECRET, "owner", password_hash)
    auth = Auth("owner", password_hash, signer, LoginLimiter())
    app.dependency_overrides[get_connector] = lambda: connector
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_auth] = lambda: auth
    with TestClient(app) as c:
        c.cookies.set(COOKIE, signer.issue(auth.clock()))
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def client(
    session: Session, fake: FakeOAuth, cipher: TokenCipher, keys: Keys
) -> Iterator[TestClient]:
    yield from make_client(session, fake, cipher, keys.sealer)


@pytest.fixture
def unsealed(session: Session, fake: FakeOAuth, cipher: TokenCipher) -> Iterator[TestClient]:
    yield from make_client(session, fake, cipher, None)


def account(session: Session, email: str = EMAIL) -> uuid.UUID:
    row = Account(provider="gmail", email=email, role="personal")
    session.add(row)
    session.commit()
    return row.id


def start(client: TestClient, account_id: uuid.UUID, purpose: str) -> dict[str, str]:
    resp = client.post(f"/accounts/{account_id}/oauth/{purpose}")
    assert resp.status_code == 200
    query = parse_qs(urlsplit(resp.json()["url"]).query)
    return {key: values[0] for key, values in query.items()}


def callback(client: TestClient, params: dict[str, str]) -> str:
    client.cookies.clear()
    resp = client.get("/oauth/google/callback", params=params, follow_redirects=False)
    assert resp.status_code == 303
    return parse_qs(urlsplit(resp.headers["location"]).query)["oauth"][0]


def credentials(session: Session, purpose: str) -> list[Credential]:
    query = sa.select(Credential).where(Credential.purpose == purpose)
    return list(session.execute(query).scalars())


@db
@pytest.mark.parametrize(
    ("purpose", "result"), [("write", "actions_enabled"), ("calendar", "calendar_connected")]
)
def test_a_full_grant_is_sealed_for_the_executor(
    client: TestClient,
    session: Session,
    fake: FakeOAuth,
    cipher: TokenCipher,
    keys: Keys,
    purpose: str,
    result: str,
) -> None:
    account_id = account(session)
    params = start(client, account_id, purpose)
    asked, hint = fake.asked[-1]
    assert asked == REQUESTED[purpose]  # type: ignore[index]
    assert hint == EMAIL
    assert callback(client, params) == result
    (row,) = credentials(session, purpose)
    assert row.account_id == account_id
    assert row.scopes == " ".join(sorted(GRANTED[purpose]))  # type: ignore[index]
    assert (row.needs_reauth, row.last_error) == (False, None)
    # Only the Executor's private key opens it
    token = keys.opener.open(row.ciphertext, account_id, purpose)
    assert token.startswith("fake-refresh-")
    assert token.encode() not in row.ciphertext
    # The API's read-token key cannot open it, and the API code refuses to try
    with pytest.raises(DecryptError):
        cipher.decrypt(row.ciphertext, account_id, purpose)
    with pytest.raises(ValueError):
        load_credential(session, cipher, account_id, purpose)


@db
@pytest.mark.parametrize(
    ("purpose", "granted"),
    [
        ("write", frozenset({WRITE_SCOPE, READ_SCOPE})),
        ("write", frozenset()),
        ("calendar", frozenset({CALENDAR_SCOPE})),
        ("calendar", frozenset({CALENDAR_SCOPE, OPENID, EMAIL_GRANTED, WRITE_SCOPE})),
    ],
)
def test_a_partial_or_extra_grant_stores_nothing(
    client: TestClient, session: Session, fake: FakeOAuth, purpose: str, granted: frozenset[str]
) -> None:
    account_id = account(session)
    fake.grants[REQUESTED[purpose]] = granted  # type: ignore[index]
    params = start(client, account_id, purpose)
    assert callback(client, params) == "scope"
    assert credentials(session, purpose) == []


@db
@pytest.mark.parametrize("purpose", ["write", "calendar"])
def test_another_google_account_stores_nothing(
    client: TestClient, session: Session, fake: FakeOAuth, purpose: str
) -> None:
    account_id = account(session)
    params = start(client, account_id, purpose)
    fake.email = OTHER
    assert callback(client, params) == "wrong_account"
    assert credentials(session, purpose) == []


@db
def test_the_address_match_ignores_case(
    client: TestClient, session: Session, fake: FakeOAuth
) -> None:
    account_id = account(session)
    params = start(client, account_id, "write")
    fake.email = EMAIL.upper()
    assert callback(client, params) == "actions_enabled"


@db
def test_without_the_public_key_the_flows_are_off(unsealed: TestClient, session: Session) -> None:
    account_id = account(session)
    resp = unsealed.post(f"/accounts/{account_id}/oauth/write")
    assert resp.status_code == 409
    (shown,) = unsealed.get("/accounts").json()
    assert shown["can_enable"] is False


@db
def test_start_checks_the_account_and_purpose(client: TestClient, session: Session) -> None:
    assert client.post(f"/accounts/{uuid.uuid4()}/oauth/write").status_code == 404
    account_id = account(session)
    assert client.post(f"/accounts/{account_id}/oauth/read").status_code == 422
    assert client.post(f"/accounts/{account_id}/oauth/send").status_code == 422


@db
def test_start_needs_login(client: TestClient, session: Session) -> None:
    account_id = account(session)
    client.cookies.clear()
    assert client.post(f"/accounts/{account_id}/oauth/write").status_code == 401


@db
def test_a_used_state_cannot_be_replayed(client: TestClient, session: Session) -> None:
    account_id = account(session)
    params = start(client, account_id, "write")
    assert callback(client, params) == "actions_enabled"
    assert callback(client, params) == "expired"


@db
def test_reconnecting_clears_the_flags(client: TestClient, session: Session) -> None:
    account_id = account(session)
    callback(client, start(client, account_id, "write"))
    session.execute(
        sa.update(Credential)
        .where(Credential.purpose == "write")
        .values(needs_reauth=True, last_error="invalid_grant")
    )
    session.commit()
    params = start_again(client, account_id)
    assert callback(client, params) == "actions_enabled"
    (row,) = credentials(session, "write")
    session.refresh(row)
    assert (row.needs_reauth, row.last_error) == (False, None)


def start_again(client: TestClient, account_id: uuid.UUID) -> dict[str, str]:
    """The callback cleared the cookie; sign in again, then start."""
    password_hash = hash_password("unused", n=2**4)
    signer = SessionSigner(SECRET, "owner", password_hash)
    auth = Auth("owner", password_hash, signer, LoginLimiter())
    app.dependency_overrides[get_auth] = lambda: auth
    client.cookies.set(COOKIE, signer.issue(auth.clock()))
    return start(client, account_id, "write")


@db
def test_accounts_show_each_grant_state(
    client: TestClient, session: Session, fake: FakeOAuth
) -> None:
    account_id = account(session)
    callback(client, start_again(client, account_id))
    session.execute(
        sa.update(Credential)
        .where(Credential.purpose == "write")
        .values(authorized_at=NOW - timedelta(days=6))
    )
    session.commit()
    start_again(client, account_id)
    (shown,) = client.get("/accounts").json()
    assert shown["can_enable"] is True
    assert shown["calendar"] is None
    write = shown["write"]
    assert write["status"] == "expiring"
    assert write["last_error"] is None
    assert "ciphertext" not in str(shown)


@db
def test_a_flagged_grant_needs_reconnect(client: TestClient, session: Session) -> None:
    account_id = account(session)
    callback(client, start_again(client, account_id))
    session.execute(
        sa.update(Credential)
        .where(Credential.purpose == "write")
        .values(needs_reauth=True, last_error="invalid_grant")
    )
    session.commit()
    start_again(client, account_id)
    (shown,) = client.get("/accounts").json()
    assert shown["write"]["status"] == "needs_reauth"
    assert shown["write"]["last_error"] == "invalid_grant"


@db
def test_the_read_flow_is_unchanged(client: TestClient, session: Session, fake: FakeOAuth) -> None:
    resp = client.post("/oauth/google/start", json={"role": "school"})
    assert resp.status_code == 200
    asked, hint = fake.asked[-1]
    assert (asked, hint) == ((READ_SCOPE,), None)
    query = parse_qs(urlsplit(resp.json()["url"]).query)
    params = {key: values[0] for key, values in query.items()}
    assert callback(client, params) == "connected"
    assert len(credentials(session, "read")) == 1
    assert credentials(session, "write") == []
