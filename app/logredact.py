"""Redact credentials from log output.

This is a safety net, not the main defense: code must never log tokens in the
first place. The filter rewrites each record's final message, so values passed
as format arguments are caught too.
"""

import copy
import logging
import re
from collections.abc import Mapping
from typing import Any

MASK = "[REDACTED]"

PATTERNS = [
    # Google OAuth access tokens and refresh tokens
    re.compile(r"ya29\.[0-9A-Za-z_\-]+"),
    re.compile(r"1//[0-9A-Za-z_\-]+"),
    # Authorization headers
    re.compile(r"(?i)(bearer\s+)[0-9A-Za-z._~+/\-]+=*"),
    # key=value or "key": "value" pairs for secret-looking keys
    re.compile(
        r"(?i)(\"?(?:access_token|refresh_token|id_token|client_secret|api_key)\"?"
        r"\s*[:=]\s*\"?)[^\s\"&,}]+"
    ),
    # OAuth authorization codes in callback URLs (not "status code: 404")
    re.compile(r"([?&]code=)[^&\s]+"),
]

# Loggers that keep their own handlers instead of using the root's
OWN_HANDLERS = ("uvicorn", "uvicorn.error", "uvicorn.access")


def redact(text: str) -> str:
    out = text
    for pattern in PATTERNS:
        if pattern.groups:
            out = pattern.sub(lambda m: m.group(1) + MASK, out)
        else:
            out = pattern.sub(MASK, out)
    return out


def redact_args(args: Any) -> Any:
    """Redact each string argument, keeping the tuple or mapping shape."""
    if isinstance(args, tuple):
        return tuple(redact(a) if isinstance(a, str) else a for a in args)
    if isinstance(args, Mapping):
        return {k: redact(v) if isinstance(v, str) else v for k, v in args.items()}
    return args


class RedactingFilter(logging.Filter):
    """Redacts the final message while keeping the record's arguments.

    Some formatters read record.args directly (uvicorn's access log unpacks
    them), so arguments are redacted in place when that fully cleans the
    message. When a secret is split between the template and an argument,
    such as "refresh_token=%s", the record falls back to one flat message.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        safe = redact(message)
        if safe != message:
            self._clean(record, safe)
        if record.exc_info and not record.exc_text:
            # Tracebacks can carry tokens in exception messages
            trace = logging.Formatter().formatException(record.exc_info)
            record.exc_text = redact(trace)
        return True

    @staticmethod
    def _clean(record: logging.LogRecord, safe: str) -> None:
        trial = copy.copy(record)
        if isinstance(trial.msg, str):
            trial.msg = redact(trial.msg)
        trial.args = redact_args(trial.args)
        try:
            cleaned = trial.getMessage()
        except (TypeError, ValueError, KeyError):
            # Redacting the template can remove a placeholder ("token=%s")
            cleaned = None
        if cleaned == safe:
            record.msg = trial.msg
            record.args = trial.args
            return
        record.msg = safe
        record.args = None


def install_redaction() -> None:
    """Attach the filter to every handler that writes log output."""
    names = ("", *OWN_HANDLERS)
    for name in names:
        logger = logging.getLogger(name)
        for handler in logger.handlers:
            if not any(isinstance(f, RedactingFilter) for f in handler.filters):
                handler.addFilter(RedactingFilter())
