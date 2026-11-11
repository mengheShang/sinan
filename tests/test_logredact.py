import io
import logging
from collections.abc import Iterator

import pytest
from uvicorn.logging import AccessFormatter

from app.logredact import MASK, RedactingFilter, install_redaction, redact

# Fake values shaped like real credentials; none of them are real
ACCESS = "ya29.FakeAccessTokenForTestsOnly"
REFRESH = "1//0FakeRefreshTokenForTestsOnly"


@pytest.mark.parametrize(
    ("text", "secret"),
    [
        (f"token {ACCESS} expired", ACCESS),
        (f"stored {REFRESH}", REFRESH),
        ("Authorization: Bearer abc.def-ghi", "abc.def-ghi"),
        ('{"refresh_token": "plain-fake-value"}', "plain-fake-value"),
        ("client_secret=fake-secret-value&x=1", "fake-secret-value"),
        ("GET /oauth/callback?state=s&code=4/fake-code HTTP/1.1", "4/fake-code"),
        ("key sk-ant-api03-FakeKeyForTests_Only-1 rejected", "FakeKeyForTests"),
    ],
)
def test_redact_hides_secret(text: str, secret: str) -> None:
    out = redact(text)
    assert secret not in out
    assert MASK in out


def test_redact_keeps_ordinary_text() -> None:
    text = "sync done: 3 messages, status code: 404, history 12345"
    assert redact(text) == text


@pytest.fixture
def stream_logger() -> Iterator[tuple[logging.Logger, io.StringIO]]:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.addFilter(RedactingFilter())
    logger = logging.getLogger("sinan.test.redact")
    logger.addHandler(handler)
    logger.propagate = False
    logger.setLevel(logging.INFO)
    yield logger, stream
    logger.removeHandler(handler)


def test_filter_redacts_format_arguments(
    stream_logger: tuple[logging.Logger, io.StringIO],
) -> None:
    logger, stream = stream_logger
    logger.info("refreshed with %s", REFRESH)
    output = stream.getvalue()
    assert REFRESH not in output
    assert "refreshed with" in output


def test_filter_redacts_tracebacks(
    stream_logger: tuple[logging.Logger, io.StringIO],
) -> None:
    logger, stream = stream_logger
    try:
        raise RuntimeError(f"bad token {ACCESS}")
    except RuntimeError:
        logger.exception("refresh failed")
    output = stream.getvalue()
    assert ACCESS not in output
    assert "RuntimeError" in output


def test_install_adds_filter_once() -> None:
    root = logging.getLogger()
    handler = logging.StreamHandler(io.StringIO())
    root.addHandler(handler)
    try:
        install_redaction()
        install_redaction()
        filters = [f for f in handler.filters if isinstance(f, RedactingFilter)]
        assert len(filters) == 1
    finally:
        root.removeHandler(handler)


def access_logger() -> tuple[logging.Logger, io.StringIO]:
    """A logger wired like uvicorn's access log: the formatter unpacks record.args."""
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    fmt = '%(client_addr)s - "%(request_line)s" %(status_code)s'
    handler.setFormatter(AccessFormatter(fmt, use_colors=False))
    handler.addFilter(RedactingFilter())
    logger = logging.getLogger("sinan.test.access")
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.INFO)
    return logger, stream


ACCESS_FMT = '%s - "%s %s HTTP/%s" %d'


def test_uvicorn_access_log_still_formats() -> None:
    logger, stream = access_logger()
    logger.info(ACCESS_FMT, "127.0.0.1:5000", "GET", "/health", "1.1", 200)
    output = stream.getvalue()
    assert '127.0.0.1:5000 - "GET /health HTTP/1.1" 200' in output


def test_uvicorn_access_log_redacts_code() -> None:
    logger, stream = access_logger()
    path = "/oauth/callback?state=s&code=4/fake-code"
    logger.info(ACCESS_FMT, "127.0.0.1:5000", "GET", path, "1.1", 200)
    output = stream.getvalue()
    assert "4/fake-code" not in output
    assert "/oauth/callback?state=s&code=" in output
    assert " 200" in output


def test_secret_split_across_template_and_argument() -> None:
    record = logging.LogRecord("t", logging.INFO, "f", 1, "refresh_token=%s", ("plain",), None)
    RedactingFilter().filter(record)
    assert "plain" not in record.getMessage()


def test_clean_record_keeps_its_arguments() -> None:
    args = ("127.0.0.1:5000", "GET", "/health", "1.1", 200)
    record = logging.LogRecord("t", logging.INFO, "f", 1, ACCESS_FMT, args, None)
    RedactingFilter().filter(record)
    assert record.args == args
    assert record.msg == ACCESS_FMT
