"""Which service may hold which key (ADR 0021, decision 1).

- Only the Executor gets SINAN_EXECUTOR_PRIVATE_KEY; the API gets the public key.
- The Executor never gets SINAN_ENCRYPTION_KEY, so it cannot open read tokens.
- Each side refuses to start when handed the other side's key.

Every key here is generated for the test.
"""

from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import SecretStr, ValidationError

from app.config import ExecutorSettings, Settings
from app.crypto import generate_key
from app.sealing import generate_pair

COMPOSE = Path(__file__).resolve().parent.parent / "docker-compose.yml"
PRIVATE = "SINAN_EXECUTOR_PRIVATE_KEY"
PUBLIC = "SINAN_EXECUTOR_PUBLIC_KEY"
READ_KEY = "SINAN_ENCRYPTION_KEY"


def compose_env() -> dict[str, set[str]]:
    """Each service's environment variable names, after anchors are merged."""
    data: dict[str, Any] = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    names = {}
    for name, service in data["services"].items():
        env = service.get("environment") or {}
        names[name] = set(env)
    return names


def test_only_the_executor_gets_the_private_key() -> None:
    holders = {name for name, env in compose_env().items() if PRIVATE in env}
    assert holders == {"executor"}


def test_the_executor_cannot_open_read_tokens() -> None:
    env = compose_env()["executor"]
    assert READ_KEY not in env
    # Nor does it get the model's key: it never talks to the LLM
    assert "ANTHROPIC_API_KEY" not in env


def test_the_api_gets_the_public_key() -> None:
    env = compose_env()
    assert PUBLIC in env["api"]
    assert PUBLIC not in env["executor"]


def test_the_anchor_shares_no_environment() -> None:
    data: dict[str, Any] = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    assert "environment" not in data["x-backend"]


def test_settings_refuse_the_private_key() -> None:
    private, _ = generate_pair()
    with pytest.raises(ValidationError, match="Executor only"):
        Settings(sinan_mode="demo", sinan_executor_private_key=SecretStr(private))


def test_settings_accept_a_valid_public_key() -> None:
    _, public = generate_pair()
    settings = Settings(sinan_mode="demo", sinan_executor_public_key=public)
    assert settings.sinan_executor_public_key == public


def test_settings_refuse_a_malformed_public_key() -> None:
    with pytest.raises(ValidationError):
        Settings(sinan_mode="demo", sinan_executor_public_key="not-a-key")


def test_empty_public_key_is_unset() -> None:
    settings = Settings(sinan_mode="demo", sinan_executor_public_key="")
    assert settings.sinan_executor_public_key is None


def test_executor_refuses_the_read_key() -> None:
    key = SecretStr(generate_key())
    with pytest.raises(ValidationError, match="must not reach the Executor"):
        ExecutorSettings(sinan_mode="live", sinan_encryption_key=key)


def test_executor_accepts_its_private_key() -> None:
    private, _ = generate_pair()
    settings = ExecutorSettings(sinan_mode="live", sinan_executor_private_key=SecretStr(private))
    assert settings.sinan_executor_private_key is not None


def test_executor_refuses_a_malformed_private_key() -> None:
    with pytest.raises(ValidationError):
        ExecutorSettings(sinan_executor_private_key=SecretStr("not-a-key"))


def test_executor_starts_without_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """Empty values from Compose mean unset; actions simply stay off."""
    monkeypatch.setenv(PRIVATE, "")
    monkeypatch.setenv(READ_KEY, "")
    settings = ExecutorSettings(sinan_mode="live")
    assert settings.sinan_executor_private_key is None
    assert settings.sinan_encryption_key is None


def test_executor_settings_have_no_model_key() -> None:
    assert "anthropic_api_key" not in ExecutorSettings.model_fields
