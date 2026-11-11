"""Read-only Gmail client implementing MailReader.

It only sends GET requests, and only to the four endpoints below. It has no
method that sends, deletes, trashes, labels or changes settings (CLAUDE.md
rule 4). tests/test_gmail.py checks every request it makes.

Rate limits: Gmail answers 429, or 403 with a rate-limit reason, when a
mailbox is read too fast. Those requests are retried with exponential
backoff, as Google's usage-limit guide asks. Any other 403 fails at once.
"""

import re
import time
from collections.abc import Callable
from datetime import datetime
from typing import Any

import httpx
from pydantic import SecretStr

from app.providers.gmail_parse import parse_message
from app.providers.mail import (
    HistoryChanges,
    HistoryExpiredError,
    MailMessage,
    MailProfile,
    MailProviderError,
    MessageNotFoundError,
)

BASE = "https://gmail.googleapis.com/gmail/v1/users/me"
PROFILE_URL = f"{BASE}/profile"
HISTORY_URL = f"{BASE}/history"
MESSAGES_URL = f"{BASE}/messages"
TIMEOUT = 30.0
PAGE_SIZE = 500

# The same exclusions as gmail_parse.wanted, applied on Gmail's side
WINDOW_QUERY = "-in:spam -in:trash -in:drafts -in:sent"


# Retries after the first try, and the waits before each, in seconds
BACKOFF = (1.0, 2.0, 4.0, 8.0, 16.0)
MAX_WAIT = 32.0
RATE_REASONS = frozenset({"rateLimitExceeded", "userRateLimitExceeded"})
# Google reason codes are short camelCase words; anything else is not kept
SAFE_REASON = re.compile(r"^[A-Za-z]{1,64}$")

Sleep = Callable[[float], None]


class GmailError(MailProviderError):
    """Gmail refused or failed a request.

    Holds the HTTP status and Google's reason code, such as
    "http_403:userRateLimitExceeded". Never a response body.
    """


def error_reason(resp: httpx.Response) -> str | None:
    """Google's machine-readable reason code from an error body, if it is safe."""
    try:
        body = resp.json()
    except ValueError:
        return None
    if not isinstance(body, dict):
        return None
    error = body.get("error")
    if not isinstance(error, dict):
        return None
    errors = error.get("errors")
    reason = None
    if isinstance(errors, list) and errors and isinstance(errors[0], dict):
        reason = errors[0].get("reason")
    if isinstance(reason, str) and SAFE_REASON.match(reason):
        return reason
    return None


def is_rate_limited(resp: httpx.Response) -> bool:
    if resp.status_code == 429:
        return True
    return resp.status_code == 403 and error_reason(resp) in RATE_REASONS


def retry_wait(resp: httpx.Response, default: float) -> float:
    """Retry-After in seconds when Gmail sends one, capped; otherwise the backoff step."""
    value = resp.headers.get("retry-after", "")
    if value.isdigit():
        return min(float(value), MAX_WAIT)
    return default


def status_code(resp: httpx.Response) -> str:
    reason = error_reason(resp)
    code = f"http_{resp.status_code}"
    return code if reason is None else f"{code}:{reason}"


class GmailReader:
    def __init__(
        self,
        access_token: SecretStr,
        http: httpx.Client | None = None,
        sleep: Sleep = time.sleep,
    ) -> None:
        self._token = access_token
        self._http = http or httpx.Client(timeout=TIMEOUT)
        self._sleep = sleep

    def _send(self, url: str, params: dict[str, str] | None) -> httpx.Response:
        headers = {"Authorization": f"Bearer {self._token.get_secret_value()}"}
        try:
            return self._http.get(url, params=params, headers=headers)
        except httpx.HTTPError as exc:
            raise GmailError("Gmail unreachable") from exc

    def _get(self, url: str, params: dict[str, str] | None = None) -> httpx.Response:
        """GET, retrying rate-limited answers with backoff. Returns the last answer."""
        resp = self._send(url, params)
        for step in BACKOFF:
            if not is_rate_limited(resp):
                return resp
            self._sleep(retry_wait(resp, step))
            resp = self._send(url, params)
        return resp

    def _json(self, resp: httpx.Response) -> dict[str, Any]:
        if resp.status_code != 200:
            raise GmailError(status_code(resp))
        body = resp.json()
        if not isinstance(body, dict):
            raise GmailError("malformed response")
        return body

    def get_profile(self) -> MailProfile:
        body = self._json(self._get(PROFILE_URL))
        return MailProfile(email=str(body["emailAddress"]), history_id=str(body["historyId"]))

    def list_history(self, start_history_id: str) -> HistoryChanges:
        params = {
            "startHistoryId": start_history_id,
            "historyTypes": "messageAdded",
            "maxResults": str(PAGE_SIZE),
        }
        ids: list[str] = []
        seen: set[str] = set()
        latest = start_history_id
        while True:
            resp = self._get(HISTORY_URL, params)
            if resp.status_code == 404:
                raise HistoryExpiredError(start_history_id)
            body = self._json(resp)
            for record in body.get("history", []):
                for added in record.get("messagesAdded", []):
                    msg_id = str(added["message"]["id"])
                    if msg_id not in seen:
                        seen.add(msg_id)
                        ids.append(msg_id)
            latest = str(body.get("historyId", latest))
            token = body.get("nextPageToken")
            if not token:
                return HistoryChanges(added_ids=ids, history_id=latest)
            params["pageToken"] = str(token)

    def list_message_ids(self, since: datetime) -> list[str]:
        after = int(since.timestamp())
        params = {"q": f"after:{after} {WINDOW_QUERY}", "maxResults": str(PAGE_SIZE)}
        ids: list[str] = []
        while True:
            body = self._json(self._get(MESSAGES_URL, params))
            ids.extend(str(m["id"]) for m in body.get("messages", []))
            token = body.get("nextPageToken")
            if not token:
                break
            params["pageToken"] = str(token)
        # Gmail lists newest first; sync stores oldest first
        ids.reverse()
        return ids

    def get_message(self, provider_msg_id: str) -> MailMessage:
        # Gmail IDs are hex; anything else must not become part of the path
        if not provider_msg_id.isascii() or not provider_msg_id.isalnum():
            raise MessageNotFoundError(provider_msg_id)
        url = f"{MESSAGES_URL}/{provider_msg_id}"
        resp = self._get(url, {"format": "full"})
        if resp.status_code == 404:
            raise MessageNotFoundError(provider_msg_id)
        return parse_message(self._json(resp))
