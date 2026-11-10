"""Connect-flow API tests against a throwaway database and a fake Google."""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session

from app.auth import Auth, LoginLimiter, SessionSigner, hash_password
from app.crypto import TokenCipher, generate_key
from app.db.models import Account, Credential, SyncState
from app.oauth import PendingFlows
from app.providers.fakes import FakeOAuth
from app.providers.oauth import READ_SCOPE
from apps.api.auth import COOKIE, get_auth
from apps.api.main import app
from apps.api.oauth import Connector, get_connector, get_session

pytestmark = pytest.mark.db

EMAIL = "student@example.edu"
REDIRECT = "http://127.0.0.1:8000/oauth/google/callback"
DASHBOARD = "http://127.0.0.1:3000"
SECRET = "fake-session-secret-for-tests-only-0123456789"


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def fake() -> FakeOAuth:
    return FakeOAuth(EMAIL, REDIRECT)


@pytest.fixture
def session(conn: Connection) -> Session:
    return Session(bind=conn, join_transaction_mode="create_savepoint")


@pytest.fixture
def client(session: Session, fake: FakeOAuth, clock: Clock) -> Iterator[TestClient]:
    connector = Connector(fake, TokenCipher(generate_key()), PendingFlows(), DASHBOARD, clock)
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


def start(client: TestClient, role: str = "school") -> dict[str, str]:
    """Begin a flow and return the callback query the fake Google would send."""
    resp = client.post("/oauth/google/start", json={"role": role})
    assert resp.status_code == 200
    query = parse_qs(urlsplit(resp.json()["url"]).query)
    return {key: values[0] for key, values in query.items()}


def callback(client: TestClient, params: dict[str, str]) -> str:
    """Hit the callback without the login cookie, as Google's redirect would."""
    client.cookies.clear()
    resp = client.get("/oauth/google/callback", params=params, follow_redirects=False)
    assert resp.status_code == 303
    location = resp.headers["location"]
    assert location.startswith(f"{DASHBOARD}/accounts?")
    result: str = parse_qs(urlsplit(location).query)["oauth"][0]
    return result


def count(session: Session, model: type[Account] | type[Credential]) -> int:
    query = sa.select(sa.func.count()).select_from(model)
    total: int = session.execute(query).scalar_one()
    return total


def test_start_requires_login(client: TestClient) -> None:
    client.cookies.clear()
    resp = client.post("/oauth/google/start", json={"role": "school"})
    assert resp.status_code == 401


def test_start_rejects_unknown_role(client: TestClient) -> None:
    resp = client.post("/oauth/google/start", json={"role": "work"})
    assert resp.status_code == 422


def test_success_stores_account_and_encrypted_token(
    client: TestClient,
    session: Session,
) -> None:
    params = start(client)
    assert callback(client, params) == "connected"
    account = session.execute(sa.select(Account)).scalar_one()
    cred = session.execute(sa.select(Credential)).scalar_one()
    sync = session.execute(sa.select(SyncState)).scalar_one()
    assert account.email == EMAIL
    assert account.role == "school"
    assert cred.purpose == "read"
    assert cred.scopes == READ_SCOPE
    assert b"fake-refresh" not in cred.ciphertext
    assert sync.needs_reauth is False


def test_wrong_state_stores_nothing(client: TestClient, session: Session) -> None:
    params = start(client)
    params["state"] = "forged-state"
    assert callback(client, params) == "expired"
    assert count(session, Account) == 0


def test_state_cannot_be_replayed(client: TestClient, session: Session) -> None:
    params = start(client)
    assert callback(client, params) == "connected"
    assert callback(client, params) == "expired"
    assert count(session, Account) == 1


def test_expired_state_stores_nothing(
    client: TestClient,
    session: Session,
    clock: Clock,
) -> None:
    params = start(client)
    clock.now += timedelta(minutes=11)
    assert callback(client, params) == "expired"
    assert count(session, Account) == 0


def test_user_denial_stores_nothing(client: TestClient, session: Session) -> None:
    params = start(client)
    denied = {"state": params["state"], "error": "access_denied"}
    assert callback(client, denied) == "denied"
    assert count(session, Account) == 0
    # The state is used up even though the user said no
    assert callback(client, params) == "expired"


@pytest.mark.parametrize(
    "granted",
    [
        frozenset(),
        frozenset({READ_SCOPE, "https://www.googleapis.com/auth/gmail.modify"}),
    ],
)
def test_wrong_scopes_store_nothing(
    client: TestClient,
    session: Session,
    fake: FakeOAuth,
    granted: frozenset[str],
) -> None:
    fake.scopes = granted
    params = start(client)
    assert callback(client, params) == "scope"
    assert count(session, Account) == 0
    assert count(session, Credential) == 0


def test_invalid_grant_sets_needs_reauth(
    client: TestClient,
    session: Session,
    fake: FakeOAuth,
) -> None:
    params = start(client)
    assert callback(client, params) == "connected"
    fake.revoked.add("fake-refresh-1")
    client.cookies.set(COOKIE, login_cookie())
    account_id = session.execute(sa.select(Account.id)).scalar_one()
    resp = client.post(f"/accounts/{account_id}/check")
    assert resp.json() == {"status": "needs_reauth"}
    listed = client.get("/accounts").json()
    assert listed[0]["status"] == "needs_reauth"
    assert listed[0]["last_error"] == "invalid_grant"


def test_reconnect_clears_needs_reauth(
    client: TestClient,
    session: Session,
    fake: FakeOAuth,
) -> None:
    assert callback(client, start(client)) == "connected"
    fake.revoked.add("fake-refresh-1")
    client.cookies.set(COOKIE, login_cookie())
    account_id = session.execute(sa.select(Account.id)).scalar_one()
    client.post(f"/accounts/{account_id}/check")
    client.cookies.set(COOKIE, login_cookie())
    assert callback(client, start(client, "personal")) == "connected"
    client.cookies.set(COOKIE, login_cookie())
    listed = client.get("/accounts").json()
    assert len(listed) == 1
    assert listed[0]["status"] == "ok"
    assert listed[0]["role"] == "personal"


def test_check_with_valid_token(client: TestClient, session: Session) -> None:
    assert callback(client, start(client)) == "connected"
    client.cookies.set(COOKIE, login_cookie())
    account_id = session.execute(sa.select(Account.id)).scalar_one()
    resp = client.post(f"/accounts/{account_id}/check")
    assert resp.json() == {"status": "ok"}


def test_check_unknown_account_is_404(client: TestClient) -> None:
    resp = client.post("/accounts/00000000-0000-4000-8000-000000000009/check")
    assert resp.status_code == 404


def test_token_age_warning(client: TestClient, clock: Clock) -> None:
    assert callback(client, start(client)) == "connected"
    client.cookies.set(COOKIE, login_cookie())
    clock.now += timedelta(days=5)
    listed = client.get("/accounts").json()
    assert listed[0]["status"] == "expiring"
    clock.now += timedelta(days=2)
    listed = client.get("/accounts").json()
    assert listed[0]["status"] == "expired"


def test_accounts_requires_login(client: TestClient) -> None:
    client.cookies.clear()
    assert client.get("/accounts").status_code == 401


def login_cookie() -> str:
    """A fresh session token from the overridden Auth."""
    auth: Auth = app.dependency_overrides[get_auth]()
    return auth.signer.issue(auth.clock())
