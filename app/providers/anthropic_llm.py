"""Extractor backed by the Claude API.

One plain Messages request per email: the versioned prompt as the system
prompt, the prepared email as the only user message, and a JSON Schema
structured output. No tools are ever sent (CLAUDE.md, rule 3), and the reply
text goes to deterministic validation before anything is stored.

The link step (ADR 0019) is the same kind of request with its own prompt,
its own one-field schema and a small token limit.

Errors carry short codes only. API error messages can echo the request, which
holds email text, so they are never logged or stored.
"""

from typing import Any, Protocol, cast

import anthropic
from pydantic import SecretStr

from app.extraction.link_reply import LINK_SCHEMA
from app.extraction.schema import OUTPUT_SCHEMA
from app.providers.llm import ExtractorError

MAX_TOKENS = 8000
# The link reply is {"target": <number> | null}
LINK_MAX_TOKENS = 100
TIMEOUT_SECONDS = 60.0
# Extraction from one email is a simple task (M0 item 7, settled in M2)
EFFORT = "low"


class Messages(Protocol):
    def create(self, **kwargs: Any) -> Any: ...


def request(
    model: str,
    prompt: str,
    text: str,
    schema: dict[str, Any] = OUTPUT_SCHEMA,
    max_tokens: int = MAX_TOKENS,
) -> dict[str, Any]:
    """The full request body. Kept separate so tests can check it has no tools."""
    return {
        "model": model,
        "max_tokens": max_tokens,
        "system": prompt,
        "messages": [{"role": "user", "content": text}],
        "output_config": {
            "effort": EFFORT,
            "format": {"type": "json_schema", "schema": schema},
        },
    }


def reply_text(response: Any) -> str:
    """The JSON text of a finished reply, or an error for refusals and cut-offs."""
    if response.stop_reason == "refusal":
        raise ExtractorError("refusal", "message")
    if response.stop_reason == "max_tokens":
        raise ExtractorError("max_tokens", "message")
    for block in response.content:
        if block.type == "text":
            return str(block.text)
    raise ExtractorError("no_text", "message")


def error_for(exc: anthropic.APIError) -> ExtractorError:
    """Map an SDK error to a code. Only an email that is too large is its own fault.

    Everything else, including a 400, may be a setting that affects every
    email, so it stops the cycle and nothing is recorded.
    """
    if isinstance(exc, anthropic.APIConnectionError):
        return ExtractorError("connection", "service")
    if isinstance(exc, anthropic.RateLimitError):
        return ExtractorError("rate_limited", "service")
    if isinstance(exc, anthropic.APIStatusError):
        if exc.status_code == 413:
            return ExtractorError("too_large", "message")
        return ExtractorError(f"http_{exc.status_code}", "service")
    return ExtractorError(type(exc).__name__, "service")


def client_messages(api_key: SecretStr) -> Messages:
    """The SDK's Messages resource, with the key passed in explicitly.

    An explicit key means the SDK never falls back to a profile on disk.
    """
    client = anthropic.Anthropic(
        api_key=api_key.get_secret_value(),
        timeout=TIMEOUT_SECONDS,
        max_retries=2,
    )
    # The SDK's typed overloads are narrower than the Protocol; the call is the same
    return cast(Messages, client.messages)


class AnthropicExtractor:
    def __init__(self, api_key: SecretStr, model: str, messages: Messages | None = None) -> None:
        self.model = model
        self._messages = messages or client_messages(api_key)

    def extract(self, prompt: str, text: str) -> str:
        body = request(self.model, prompt, text)
        try:
            response = self._messages.create(**body)
        except anthropic.APIError as exc:
            raise error_for(exc) from None
        return reply_text(response)


class AnthropicLinkPicker:
    """The link step: one request per change, the same way as extraction."""

    def __init__(self, api_key: SecretStr, model: str, messages: Messages | None = None) -> None:
        self.model = model
        self._messages = messages or client_messages(api_key)

    def pick(self, prompt: str, text: str) -> str:
        body = request(self.model, prompt, text, LINK_SCHEMA, LINK_MAX_TOKENS)
        try:
            response = self._messages.create(**body)
        except anthropic.APIError as exc:
            raise error_for(exc) from None
        return reply_text(response)
