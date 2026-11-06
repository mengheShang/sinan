"""Login and session tests. Hashes use a low scrypt cost so the suite stays fast."""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.auth import (
    Auth,
    InvalidHashError,
    LoginLimiter,
    SessionSigner,
    generate_secret,
    hash_password,
    verify_password,
)
from app.config import Settings
from app.crypto import generate_key
from apps.api.auth import COOKIE, DEMO_PASSWORD, build_auth, get_auth
from apps.api.main import app

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
    signer = SessionSigner(SECRET, password_hash)
    limiter = LoginLimiter(max_failures=3, window=60)
    return Auth(password_hash, signer, limiter, clock)


@pytest.fixture
def client(auth: Auth) -> Iterator[TestClient]:
    app.dependency_overrides[get_auth] = lambda: auth
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def login(client: TestClient, password: str = PASSWORD) -> int:
    resp = client.post("/auth/login", json={"password": password})
    code: int = resp.status_code
    return code


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


# Session tokens


def test_token_round_trip(clock: FakeClock) -> None:
    signer = SessionSigner(SECRET, "h")
    token = signer.issue(clock.now)
    assert signer.check(token, clock.now)


def test_token_expires(clock: FakeClock) -> None:
    signer = SessionSigner(SECRET, "h", ttl=60)
    token = signer.issue(clock.now)
    assert not signer.check(token, clock.now + 60)


@pytest.mark.parametrize("bad", ["", "v1", "v1.a.b.c", "v2.1.n.sig", "v1.1.n.é"])
def test_garbage_tokens_fail(bad: str, clock: FakeClock) -> None:
    signer = SessionSigner(SECRET, "h")
    assert not signer.check(bad, clock.now)


def test_tampered_expiry_fails(clock: FakeClock) -> None:
    signer = SessionSigner(SECRET, "h", ttl=60)
    parts = signer.issue(clock.now).split(".")
    parts[1] = str(int(parts[1]) + 10_000)
    assert not signer.check(".".join(parts), clock.now + 120)


def test_new_password_ends_old_sessions(clock: FakeClock) -> None:
    old = SessionSigner(SECRET, "old-hash")
    new = SessionSigner(SECRET, "new-hash")
    assert not new.check(old.issue(clock.now), clock.now)


def test_new_secret_ends_old_sessions(clock: FakeClock) -> None:
    old = SessionSigner(SECRET, "h")
    new = SessionSigner(generate_secret(), "h")
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


def test_wrong_password_is_401_without_detail(client: TestClient) -> None:
    resp = client.post("/auth/login", json={"password": "wrong"})
    assert resp.status_code == 401
    assert resp.json() == {"detail": "Login failed"}
    assert COOKIE not in resp.cookies


def test_correct_password_gives_access(client: TestClient) -> None:
    resp = client.post("/auth/login", json={"password": PASSWORD})
    assert resp.status_code == 200
    assert client.get("/auth/session").status_code == 200


def test_cookie_flags(client: TestClient) -> None:
    resp = client.post("/auth/login", json={"password": PASSWORD})
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
    client.cookies.set(COOKIE, token[:-2] + "xx")
    assert client.get("/auth/session").status_code == 401


def test_logout_clears_cookie(client: TestClient) -> None:
    assert login(client) == 200
    client.post("/auth/logout")
    assert client.get("/auth/session").status_code == 401


def test_login_is_rate_limited(client: TestClient, clock: FakeClock) -> None:
    for _ in range(3):
        assert login(client, "wrong") == 401
    resp = client.post("/auth/login", json={"password": PASSWORD})
    assert resp.status_code == 429
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


def test_demo_uses_demo_password() -> None:
    built = build_auth(Settings(sinan_mode="demo"))
    assert verify_password(DEMO_PASSWORD, built.password_hash)


def test_live_requires_password_hash() -> None:
    settings = Settings(sinan_mode="live", sinan_encryption_key=SecretStr(generate_key()))
    with pytest.raises(ValueError, match="PASSWORD_HASH"):
        build_auth(settings)


def test_live_requires_session_secret() -> None:
    settings = Settings(
        sinan_mode="live",
        sinan_encryption_key=SecretStr(generate_key()),
        sinan_dashboard_password_hash=SecretStr(hash_password(PASSWORD, **FAST)),
    )
    with pytest.raises(ValueError, match="SESSION_SECRET"):
        build_auth(settings)


def test_short_secret_is_rejected() -> None:
    settings = Settings(
        sinan_mode="demo",
        sinan_session_secret=SecretStr("short"),
    )
    with pytest.raises(ValueError, match="32 characters"):
        build_auth(settings)


def test_live_accepts_full_setup() -> None:
    settings = Settings(
        sinan_mode="live",
        sinan_encryption_key=SecretStr(generate_key()),
        sinan_dashboard_password_hash=SecretStr(hash_password(PASSWORD, **FAST)),
        sinan_session_secret=SecretStr(SECRET),
    )
    built = build_auth(settings)
    assert verify_password(PASSWORD, built.password_hash)


def test_empty_env_values_count_as_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SINAN_DASHBOARD_PASSWORD_HASH", "")
    monkeypatch.setenv("SINAN_SESSION_SECRET", "")
    settings = Settings(sinan_mode="demo")
    assert settings.sinan_dashboard_password_hash is None
    assert settings.sinan_session_secret is None
