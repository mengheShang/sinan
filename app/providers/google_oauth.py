"""Google OAuth for a Web application client, written directly against httpx.

Endpoints and parameters follow Google's "OAuth 2.0 for Web Server
Applications" guide. PKCE (S256) is used on top of the client secret.
"""

from urllib.parse import urlencode

import httpx
from pydantic import SecretStr

from app.providers.oauth import READ_SCOPE, OAuthError, ReauthRequiredError, TokenGrant

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"  # noqa: S105 - an endpoint, not a secret
PROFILE_URL = "https://gmail.googleapis.com/gmail/v1/users/me/profile"
USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
TIMEOUT = 15.0


def error_code(resp: httpx.Response) -> str:
    """The OAuth `error` field, or the HTTP status. Never the response body."""
    try:
        body = resp.json()
    except ValueError:
        return f"http_{resp.status_code}"
    code = body.get("error") if isinstance(body, dict) else None
    if isinstance(code, str) and code.isascii() and len(code) <= 64:
        return code
    return f"http_{resp.status_code}"


class GoogleOAuth:
    def __init__(
        self,
        client_id: str,
        client_secret: SecretStr,
        redirect_uri: str,
        http: httpx.Client | None = None,
    ) -> None:
        self.client_id = client_id
        self._secret = client_secret
        self.redirect_uri = redirect_uri
        self._http = http or httpx.Client(timeout=TIMEOUT)

    def authorize_url(
        self,
        state: str,
        challenge: str,
        scopes: tuple[str, ...] = (READ_SCOPE,),
        login_hint: str | None = None,
    ) -> str:
        params = {
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "response_type": "code",
            "scope": " ".join(scopes),
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            # Ask for a refresh token, and ask again on every reconnect
            "access_type": "offline",
            "prompt": "consent",
            "include_granted_scopes": "false",
        }
        if login_hint is not None:
            params["login_hint"] = login_hint
        return f"{AUTH_URL}?{urlencode(params)}"

    def _post_token(self, form: dict[str, str]) -> dict[str, object]:
        form["client_id"] = self.client_id
        form["client_secret"] = self._secret.get_secret_value()
        try:
            resp = self._http.post(TOKEN_URL, data=form)
        except httpx.HTTPError as exc:
            raise OAuthError("token endpoint unreachable") from exc
        if resp.status_code != 200:
            code = error_code(resp)
            if code == "invalid_grant":
                raise ReauthRequiredError(code)
            raise OAuthError(code)
        body = resp.json()
        if not isinstance(body, dict):
            raise OAuthError("malformed token response")
        return body

    def exchange(self, code: str, verifier: str) -> TokenGrant:
        form = {
            "grant_type": "authorization_code",
            "code": code,
            "code_verifier": verifier,
            "redirect_uri": self.redirect_uri,
        }
        body = self._post_token(form)
        refresh = body.get("refresh_token")
        access = body.get("access_token")
        scope = body.get("scope")
        if not isinstance(refresh, str) or not isinstance(access, str):
            raise OAuthError("token response is missing a token")
        scopes = frozenset(scope.split()) if isinstance(scope, str) else frozenset()
        return TokenGrant(SecretStr(refresh), SecretStr(access), scopes)

    def refresh(self, refresh_token: SecretStr) -> SecretStr:
        form = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token.get_secret_value(),
        }
        body = self._post_token(form)
        access = body.get("access_token")
        if not isinstance(access, str):
            raise OAuthError("token response is missing a token")
        return SecretStr(access)

    def mailbox_email(self, access_token: SecretStr) -> str:
        headers = {"Authorization": f"Bearer {access_token.get_secret_value()}"}
        try:
            resp = self._http.get(PROFILE_URL, headers=headers)
        except httpx.HTTPError as exc:
            raise OAuthError("Gmail profile unreachable") from exc
        if resp.status_code != 200:
            raise OAuthError(error_code(resp))
        body = resp.json()
        email = body.get("emailAddress") if isinstance(body, dict) else None
        if not isinstance(email, str) or "@" not in email:
            raise OAuthError("Gmail profile has no address")
        return email

    def account_email(self, access_token: SecretStr) -> str:
        headers = {"Authorization": f"Bearer {access_token.get_secret_value()}"}
        try:
            resp = self._http.get(USERINFO_URL, headers=headers)
        except httpx.HTTPError as exc:
            raise OAuthError("userinfo unreachable") from exc
        if resp.status_code != 200:
            raise OAuthError(error_code(resp))
        body = resp.json()
        if not isinstance(body, dict) or body.get("email_verified") is not True:
            raise OAuthError("userinfo has no verified address")
        email = body.get("email")
        if not isinstance(email, str) or "@" not in email:
            raise OAuthError("userinfo has no verified address")
        return email
