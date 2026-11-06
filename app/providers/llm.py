"""LLM extraction interface.

The model gets text in and returns raw JSON text out. It is never given tools
or any way to act (CLAUDE.md, rule 3). Deterministic code validates the JSON.
"""

from typing import Protocol


class ExtractorError(Exception):
    """The model call failed, for example a timeout or an API error."""


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
