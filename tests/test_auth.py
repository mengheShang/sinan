"""Login and session tests. Hashes use a low scrypt cost so the suite stays fast."""

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.auth import (
    Auth,
    InvalidHashError,
    LoginLimiter,
    SessionSigner,
    check_username,
    generate_secret,
    hash_password,
    verify_password,
)
from app.config import Settings
from app.crypto import generate_key
from apps.api.auth import COOKIE, DEMO_PASSWORD, DEMO_USERNAME, build_auth, get_auth
from apps.api.main import app

USERNAME = "owner"
PASSWORD = "correct horse battery"
SECRET = "fake-session-secret-for-tests-only-0123456789"
FAST = {"n": 2**4, "r": 8, "p": 1}
PUBLIC = {"/health", "/health/db", "/auth/login", "/auth/logout"}


class FakeClock:
    def __init__(self) -> None:
        self.now = 1_800_000_000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def auth(clock: FakeClock) -> Auth:
    password_hash = hash_password(PASSWORD, **FAST)
    signer = SessionSigner(SECRET, USERNAME, password_hash)
    limiter = LoginLimiter(max_failures=3, window=60)
    return Auth(USERNAME, password_hash, signer, limiter, clock)


@pytest.fixture
def client(auth: Auth) -> Iterator[TestClient]:
    app.dependency_overrides[get_auth] = lambda: auth
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def login(client: TestClient, username: str = USERNAME, password: str = PASSWORD) -> int:
    body = {"username": username, "password": password}
    resp = client.post("/auth/login", json=body)
    code: int = resp.status_code
    return code


def live_settings(**values: Any) -> Settings:
    return Settings(sinan_mode="live", sinan_encryption_key=SecretStr(generate_key()), **values)


# Password hashing


def test_hash_round_trip() -> None:
    encoded = hash_password(PASSWORD, **FAST)
    assert verify_password(PASSWORD, encoded)
    assert not verify_password("wrong", encoded)


def test_hash_is_salted_and_dollar_free() -> None:
    first = hash_password(PASSWORD, **FAST)
    second = hash_password(PASSWORD, **FAST)
    assert first != second
    assert "$" not in first
    assert PASSWORD not in first


def test_malformed_hash_is_rejected() -> None:
    with pytest.raises(InvalidHashError):
        verify_password(PASSWORD, "bcrypt:not-ours")


# Username


@pytest.mark.parametrize("bad", ["", "two words", "tab\tname", "x" * 65])
def test_bad_usernames_are_rejected(bad: str) -> None:
    with pytest.raises(ValueError, match="SINAN_DASHBOARD_USERNAME"):
        check_username(bad)


def test_check_login_needs_both(auth: Auth) -> None:
    assert auth.check_login(USERNAME, PASSWORD)
    assert not auth.check_login("someone-else", PASSWORD)
    assert not auth.check_login(USERNAME, "wrong")
    assert not auth.check_login("Owner", PASSWORD)


# Session tokens


def test_token_round_trip(clock: FakeClock) -> None:
    signer = SessionSigner(SECRET, USERNAME, "h")
    token = signer.issue(clock.now)
    assert signer.check(token, clock.now)


def test_token_expires(clock: FakeClock) -> None:
    signer = SessionSigner(SECRET, USERNAME, "h", ttl=60)
    token = signer.issue(clock.now)
    assert not signer.check(token, clock.now + 60)


@pytest.mark.parametrize("bad", ["", "v1", "v1.a.b.c", "v2.1.n.sig", "v1.1.n.é"])
def test_garbage_tokens_fail(bad: str, clock: FakeClock) -> None:
    signer = SessionSigner(SECRET, USERNAME, "h")
    assert not signer.check(bad, clock.now)


def test_tampered_expiry_fails(clock: FakeClock) -> None:
    signer = SessionSigner(SECRET, USERNAME, "h", ttl=60)
    parts = signer.issue(clock.now).split(".")
    parts[1] = str(int(parts[1]) + 10_000)
    assert not signer.check(".".join(parts), clock.now + 120)


def test_new_password_ends_old_sessions(clock: FakeClock) -> None:
    old = SessionSigner(SECRET, USERNAME, "old-hash")
    new = SessionSigner(SECRET, USERNAME, "new-hash")
    assert not new.check(old.issue(clock.now), clock.now)


def test_new_username_ends_old_sessions(clock: FakeClock) -> None:
    old = SessionSigner(SECRET, "owner", "h")
    new = SessionSigner(SECRET, "renamed", "h")
    assert not new.check(old.issue(clock.now), clock.now)


def test_new_secret_ends_old_sessions(clock: FakeClock) -> None:
    old = SessionSigner(SECRET, USERNAME, "h")
    new = SessionSigner(generate_secret(), USERNAME, "h")
    assert not new.check(old.issue(clock.now), clock.now)


# Limiter


def test_limiter_locks_then_unlocks() -> None:
    limiter = LoginLimiter(max_failures=2, window=60)
    limiter.fail(0)
    assert limiter.retry_after(1) == 0
    limiter.fail(1)
    assert limiter.retry_after(2) > 0
    assert limiter.retry_after(61) == 0


# API


def test_no_cookie_is_401(client: TestClient) -> None:
    assert client.get("/auth/session").status_code == 401


@pytest.mark.parametrize(
    ("username", "password"),
    [(USERNAME, "wrong"), ("someone-else", PASSWORD), ("someone-else", "wrong")],
)
def test_wrong_login_is_401_without_detail(
    client: TestClient,
    username: str,
    password: str,
) -> None:
    body = {"username": username, "password": password}
    resp = client.post("/auth/login", json=body)
    assert resp.status_code == 401
    assert resp.json() == {"detail": "Login failed"}
    assert COOKIE not in resp.cookies


def test_missing_username_is_rejected(client: TestClient) -> None:
    resp = client.post("/auth/login", json={"password": PASSWORD})
    assert resp.status_code == 422
    assert COOKIE not in resp.cookies


def test_correct_login_gives_access(client: TestClient) -> None:
    assert login(client) == 200
    assert client.get("/auth/session").status_code == 200


def test_cookie_flags(client: TestClient) -> None:
    body = {"username": USERNAME, "password": PASSWORD}
    resp = client.post("/auth/login", json=body)
    header = resp.headers["set-cookie"].lower()
    assert "httponly" in header
    assert "samesite=strict" in header
    assert "path=/" in header


def test_expired_cookie_is_401(client: TestClient, clock: FakeClock, auth: Auth) -> None:
    assert login(client) == 200
    clock.now += auth.signer.ttl
    assert client.get("/auth/session").status_code == 401


def test_tampered_cookie_is_401(client: TestClient) -> None:
    assert login(client) == 200
    token = client.cookies[COOKIE]
    # Clear first: httpx2 adds a second cookie instead of replacing the first
    client.cookies.clear()
    client.cookies.set(COOKIE, token[:-2] + "xx")
    assert client.get("/auth/session").status_code == 401


def test_logout_clears_cookie(client: TestClient) -> None:
    assert login(client) == 200
    client.post("/auth/logout")
    assert client.get("/auth/session").status_code == 401


def test_login_is_rate_limited(client: TestClient, clock: FakeClock) -> None:
    assert login(client, password="wrong") == 401
    assert login(client, username="someone-else") == 401
    assert login(client, password="wrong") == 401
    assert login(client) == 429
    body = {"username": USERNAME, "password": PASSWORD}
    resp = client.post("/auth/login", json=body)
    assert int(resp.headers["retry-after"]) > 0
    clock.now += 61
    assert login(client) == 200


def test_health_stays_public(client: TestClient) -> None:
    assert client.get("/health").status_code == 200


def test_every_other_route_requires_a_session(client: TestClient) -> None:
    paths = app.openapi()["paths"]
    private = {p: ops for p, ops in paths.items() if p not in PUBLIC}
    assert private, "expected at least one protected route"
    for path, ops in private.items():
        for method in ops:
            resp = client.request(method, path)
            assert resp.status_code == 401, f"{method} {path}"


def test_public_routes_are_only_the_expected_ones(client: TestClient) -> None:
    paths = app.openapi()["paths"]
    open_paths = set()
    for path, ops in paths.items():
        for method in ops:
            if client.request(method, path).status_code != 401:
                open_paths.add(path)
    assert open_paths == PUBLIC


# Configuration


def test_demo_uses_demo_login() -> None:
    built = build_auth(Settings(sinan_mode="demo"))
    assert built.check_login(DEMO_USERNAME, DEMO_PASSWORD)


def test_live_requires_username() -> None:
    settings = live_settings(
        sinan_dashboard_password_hash=SecretStr(hash_password(PASSWORD, **FAST)),
        sinan_session_secret=SecretStr(SECRET),
    )
    with pytest.raises(ValueError, match="USERNAME"):
        build_auth(settings)


def test_live_requires_password_hash() -> None:
    settings = live_settings(sinan_dashboard_username=USERNAME)
    with pytest.raises(ValueError, match="PASSWORD_HASH"):
        build_auth(settings)


def test_live_requires_session_secret() -> None:
    settings = live_settings(
        sinan_dashboard_username=USERNAME,
        sinan_dashboard_password_hash=SecretStr(hash_password(PASSWORD, **FAST)),
    )
    with pytest.raises(ValueError, match="SESSION_SECRET"):
        build_auth(settings)


def test_live_rejects_bad_username() -> None:
    settings = live_settings(
        sinan_dashboard_username="two words",
        sinan_dashboard_password_hash=SecretStr(hash_password(PASSWORD, **FAST)),
        sinan_session_secret=SecretStr(SECRET),
    )
    with pytest.raises(ValueError, match="whitespace"):
        build_auth(settings)


def test_short_secret_is_rejected() -> None:
    settings = Settings(
        sinan_mode="demo",
        sinan_session_secret=SecretStr("short"),
    )
    with pytest.raises(ValueError, match="32 characters"):
        build_auth(settings)


def test_live_accepts_full_setup() -> None:
    settings = live_settings(
        sinan_dashboard_username=USERNAME,
        sinan_dashboard_password_hash=SecretStr(hash_password(PASSWORD, **FAST)),
        sinan_session_secret=SecretStr(SECRET),
    )
    built = build_auth(settings)
    assert built.check_login(USERNAME, PASSWORD)
    assert not built.check_login(DEMO_USERNAME, DEMO_PASSWORD)


def test_empty_env_values_count_as_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SINAN_DASHBOARD_USERNAME", "")
    monkeypatch.setenv("SINAN_DASHBOARD_PASSWORD_HASH", "")
    monkeypatch.setenv("SINAN_SESSION_SECRET", "")
    settings = Settings(sinan_mode="demo")
    assert settings.sinan_dashboard_username is None
    assert settings.sinan_dashboard_password_hash is None
    assert settings.sinan_session_secret is None
