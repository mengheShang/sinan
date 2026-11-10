# ADR 0003: Read-only Gmail OAuth flow

- Status: Accepted
- Date: 2026-11-10
- Decider: Menghe

## Context

Issue #9 connects a Google account with `gmail.readonly`. The design fixes the scope and the 7-day Testing-mode expiry (M0 item 4), but four choices about the client, the callback, the warning and the code were left open.

## Decisions

### 1. A "Web application" OAuth client with one fixed redirect URI

The owner creates a Web application client and registers exactly `http://127.0.0.1:8000/oauth/google/callback`. Google's web-server guide exempts localhost IP addresses from the HTTPS and raw-IP rules. The client secret stays in `.env`, and only the API container receives it. PKCE (S256) is used on top of the secret.

A Desktop client was the alternative. Google treats its secret as public, and its guide does not say whether a loopback redirect may include a path.

### 2. The callback is checked by a one-time state, not the login cookie

The login cookie is `SameSite=Strict`, so the browser does not send it on Google's cross-site redirect back to Sinan. The callback is therefore a public route. It accepts a request only with a `state` that:

- was created by a signed-in user,
- is random (32 bytes),
- is less than 10 minutes old, and
- has not been used before.

At most 20 flows are pending at once. The PKCE verifier is tied to the same state.

Relaxing the cookie to `Lax` was the alternative. It would weaken cross-site protection for every page to help one route.

### 3. Warn two days before the 7-day expiry

An account shows "expires soon" from day 5, "expired" from day 7, and "needs reconnect" as soon as Google answers `invalid_grant`. Two days leaves room for a weekend.

### 4. OAuth code written directly against httpx

`app/providers/google_oauth.py` calls Google's token endpoint and the Gmail profile endpoint with httpx, in about 120 lines. The owner can review the scope, state and PKCE handling line by line. `google-auth-oauthlib` would hide that logic and add several dependencies.

## Consequences

- The flow requests only `gmail.readonly`, with `include_granted_scopes=false`. It stores nothing unless Google granted exactly that scope, so an unchecked box on the consent screen is caught.
- Pending flows live in the API process memory. Restarting the API cancels any half-finished connect, and the user clicks Connect again.
- The API sees the refresh token once, during the callback, and stores it encrypted, bound to its account and purpose (#7). The Worker will decrypt it in #10.
- `refresh_read_token` marks an account `needs_reauth` on `invalid_grant`, a missing token, or a token that no longer decrypts. Other errors propagate, so a later retry can succeed.
