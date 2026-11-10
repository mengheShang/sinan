"""Read-only Gmail client implementing MailReader.

It only sends GET requests, and only to the four endpoints below. It has no
method that sends, deletes, trashes, labels or changes settings (CLAUDE.md
rule 4). tests/test_gmail.py checks every request it makes.
"""

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


class GmailError(Exception):
    """Gmail refused or failed a request. Holds the HTTP status only, never a body."""


class GmailReader:
    def __init__(self, access_token: SecretStr, http: httpx.Client | None = None) -> None:
        self._token = access_token
        self._http = http or httpx.Client(timeout=TIMEOUT)

    def _get(self, url: str, params: dict[str, str] | None = None) -> httpx.Response:
        headers = {"Authorization": f"Bearer {self._token.get_secret_value()}"}
        try:
            return self._http.get(url, params=params, headers=headers)
        except httpx.HTTPError as exc:
            raise GmailError("Gmail unreachable") from exc

    def _json(self, resp: httpx.Response) -> dict[str, Any]:
        if resp.status_code != 200:
            raise GmailError(f"http_{resp.status_code}")
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
