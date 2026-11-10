"""OAuth flow rules and the Google client, against a fake token endpoint."""

import json
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from pydantic import SecretStr

from app.config import Settings
from app.crypto import generate_key
from app.oauth import PendingFlows, scopes_ok, token_status
from app.providers.fakes import FakeOAuth
from app.providers.google_oauth import PROFILE_URL, TOKEN_URL, GoogleOAuth
from app.providers.oauth import READ_SCOPE, OAuthError, ReauthRequiredError, pkce_challenge
from apps.api.oauth import build_connector

NOW = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
REDIRECT = "http://127.0.0.1:8000/oauth/google/callback"
SECRET = SecretStr("fake-client-secret")


# The scope guard required by Issue #9


def test_read_scope_is_gmail_readonly_only() -> None:
    assert READ_SCOPE == "https://www.googleapis.com/auth/gmail.readonly"


def test_authorize_url_requests_only_the_read_scope() -> None:
    client = GoogleOAuth("fake-client-id", SECRET, REDIRECT)
    url = client.authorize_url("s", "c")
    query = parse_qs(urlsplit(url).query)
    assert query["scope"] == [READ_SCOPE]
    assert query["include_granted_scopes"] == ["false"]


def test_authorize_url_parameters() -> None:
    client = GoogleOAuth("fake-client-id", SECRET, REDIRECT)
    parts = urlsplit(client.authorize_url("the-state", "the-challenge"))
    query = parse_qs(parts.query)
    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == (
        "https://accounts.google.com/o/oauth2/v2/auth"
    )
    assert query["state"] == ["the-state"]
    assert query["code_challenge"] == ["the-challenge"]
    assert query["code_challenge_method"] == ["S256"]
    assert query["redirect_uri"] == [REDIRECT]
    assert query["access_type"] == ["offline"]
    assert query["prompt"] == ["consent"]
    assert "client_secret" not in query


@pytest.mark.parametrize(
    "granted",
    [
        frozenset(),
        frozenset({"https://www.googleapis.com/auth/gmail.modify"}),
        frozenset({READ_SCOPE, "https://www.googleapis.com/auth/gmail.send"}),
        frozenset({READ_SCOPE, "openid"}),
    ],
)
def test_other_scope_sets_are_refused(granted: frozenset[str]) -> None:
    assert not scopes_ok(granted)


def test_exact_read_scope_is_accepted() -> None:
    assert scopes_ok(frozenset({READ_SCOPE}))


# PKCE and pending flows


def test_pkce_matches_rfc_7636_example() -> None:
    verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    assert pkce_challenge(verifier) == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"


def test_flow_round_trip() -> None:
    flows = PendingFlows()
    start = flows.start("school", NOW)
    flow = flows.finish(start.state, NOW)
    assert flow is not None
    assert flow.role == "school"
    assert pkce_challenge(flow.verifier) == start.challenge
    assert 43 <= len(flow.verifier) <= 128


def test_state_works_once() -> None:
    flows = PendingFlows()
    start = flows.start("school", NOW)
    assert flows.finish(start.state, NOW) is not None
    assert flows.finish(start.state, NOW) is None


def test_unknown_state_fails() -> None:
    flows = PendingFlows()
    flows.start("school", NOW)
    assert flows.finish("forged-state", NOW) is None


def test_state_expires() -> None:
    flows = PendingFlows(ttl=timedelta(minutes=10))
    start = flows.start("school", NOW)
    later = NOW + timedelta(minutes=10)
    assert flows.finish(start.state, later) is None


def test_pending_flows_are_capped() -> None:
    flows = PendingFlows(limit=2)
    first = flows.start("school", NOW)
    flows.start("school", NOW + timedelta(seconds=1))
    flows.start("school", NOW + timedelta(seconds=2))
    assert flows.finish(first.state, NOW + timedelta(seconds=3)) is None


def test_bad_role_is_rejected() -> None:
    with pytest.raises(ValueError, match="role"):
        PendingFlows().start("work", NOW)


# Token age


@pytest.mark.parametrize(
    ("age", "reauth", "expected"),
    [
        (timedelta(days=1), False, "ok"),
        (timedelta(days=4, hours=23), False, "ok"),
        (timedelta(days=5), False, "expiring"),
        (timedelta(days=6, hours=23), False, "expiring"),
        (timedelta(days=7), False, "expired"),
        (timedelta(hours=1), True, "needs_reauth"),
    ],
)
def test_token_status(age: timedelta, reauth: bool, expected: str) -> None:
    assert token_status(NOW - age, reauth, NOW) == expected


# Google client against a fake endpoint


def google(handler: httpx.MockTransport) -> GoogleOAuth:
    http = httpx.Client(transport=handler)
    return GoogleOAuth("fake-client-id", SECRET, REDIRECT, http)


def json_response(status: int, body: object) -> httpx.Response:
    return httpx.Response(status, content=json.dumps(body).encode())


def test_exchange_success() -> None:
    seen: dict[str, list[str]] = {}

    def handle(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == TOKEN_URL
        seen.update(parse_qs(request.content.decode()))
        body = {
            "access_token": "fake-access",
            "refresh_token": "fake-refresh",
            "scope": READ_SCOPE,
            "token_type": "Bearer",
            "expires_in": 3599,
        }
        return json_response(200, body)

    grant = google(httpx.MockTransport(handle)).exchange("fake-code", "fake-verifier")
    assert grant.refresh_token.get_secret_value() == "fake-refresh"
    assert grant.scopes == frozenset({READ_SCOPE})
    assert seen["grant_type"] == ["authorization_code"]
    assert seen["code_verifier"] == ["fake-verifier"]
    assert seen["redirect_uri"] == [REDIRECT]


def test_exchange_without_refresh_token_fails() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return json_response(200, {"access_token": "fake-access", "scope": READ_SCOPE})

    with pytest.raises(OAuthError, match="missing"):
        google(httpx.MockTransport(handle)).exchange("fake-code", "v")


def test_refresh_invalid_grant_needs_reauth() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        body = {"error": "invalid_grant", "error_description": "Token has been expired or revoked."}
        return json_response(400, body)

    client = google(httpx.MockTransport(handle))
    with pytest.raises(ReauthRequiredError):
        client.refresh(SecretStr("fake-refresh"))


def test_other_token_errors_are_plain_oauth_errors() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return json_response(401, {"error": "invalid_client"})

    client = google(httpx.MockTransport(handle))
    with pytest.raises(OAuthError) as caught:
        client.refresh(SecretStr("fake-refresh"))
    assert not isinstance(caught.value, ReauthRequiredError)
    assert str(caught.value) == "invalid_client"


def test_errors_never_carry_the_response_body() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, content=b"fake-refresh appears in this body")

    client = google(httpx.MockTransport(handle))
    with pytest.raises(OAuthError) as caught:
        client.refresh(SecretStr("fake-refresh"))
    assert str(caught.value) == "http_500"


def test_network_failure_is_an_oauth_error() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    with pytest.raises(OAuthError, match="unreachable"):
        google(httpx.MockTransport(handle)).exchange("fake-code", "v")


def test_mailbox_email_reads_gmail_profile() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == PROFILE_URL
        assert request.method == "GET"
        assert request.headers["authorization"] == "Bearer fake-access"
        return json_response(200, {"emailAddress": "student@example.edu", "historyId": "1"})

    client = google(httpx.MockTransport(handle))
    assert client.mailbox_email(SecretStr("fake-access")) == "student@example.edu"


# The fake used by the demo


def test_fake_checks_pkce() -> None:
    fake = FakeOAuth("student@example.edu", REDIRECT)
    url = fake.authorize_url("s", pkce_challenge("right-verifier"))
    code = parse_qs(urlsplit(url).query)["code"][0]
    with pytest.raises(OAuthError):
        fake.exchange(code, "wrong-verifier")


# Live-mode configuration


def live(**values: Any) -> Settings:
    key = SecretStr(generate_key())
    return Settings(sinan_mode="live", sinan_encryption_key=key, **values)


def test_live_requires_google_client() -> None:
    with pytest.raises(ValueError, match="GOOGLE_CLIENT_ID"):
        build_connector(live())


def test_live_uses_the_real_google_client() -> None:
    settings = live(google_client_id="fake-client-id", google_client_secret="fake-secret")
    connector = build_connector(settings)
    assert isinstance(connector.client, GoogleOAuth)


def test_demo_uses_the_fake() -> None:
    connector = build_connector(Settings(sinan_mode="demo"))
    assert isinstance(connector.client, FakeOAuth)
