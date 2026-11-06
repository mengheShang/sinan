"""Redact credentials from log output.

This is a safety net, not the main defense: code must never log tokens in the
first place. The filter rewrites each record's final message, so values passed
as format arguments are caught too.
"""

import logging
import re

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


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        record.msg = redact(message)
        record.args = None
        if record.exc_info and not record.exc_text:
            # Tracebacks can carry tokens in exception messages
            trace = logging.Formatter().formatException(record.exc_info)
            record.exc_text = redact(trace)
        return True


def install_redaction() -> None:
    """Attach the filter to every handler that writes log output."""
    names = ("", *OWN_HANDLERS)
    for name in names:
        logger = logging.getLogger(name)
        for handler in logger.handlers:
            if not any(isinstance(f, RedactingFilter) for f in handler.filters):
                handler.addFilter(RedactingFilter())
