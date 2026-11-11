"""LLM extraction interface.

The model gets text in and returns raw JSON text out. It is never given tools
or any way to act (CLAUDE.md, rule 3). Deterministic code validates the JSON.
"""

from typing import Literal, Protocol

# "message": this email caused it (a refusal, a request the API rejects).
#   Record the failure so the email is not sent again with this prompt.
# "service": the call never reached a verdict (network, rate limit, bad key).
#   Record nothing and stop the cycle, so every email is tried again later.
Scope = Literal["message", "service"]


class ExtractorError(Exception):
    """The model call failed. `code` is short and never quotes the email."""

    def __init__(self, code: str, scope: Scope) -> None:
        super().__init__(code)
        self.code = code
        self.scope = scope


class Extractor(Protocol):
    """Turns prepared email text into raw JSON. Has no tools."""

    # Model name recorded in extraction_runs
    model: str

    def extract(self, prompt: str, text: str) -> str:
        """Return the model's raw JSON output for one email.

        `prompt` is the versioned instructions, `text` is the untrusted email
        content. Raises ExtractorError when the call fails.
        """
        ...
