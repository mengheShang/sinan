"""Runtime settings. Read from environment variables only, never from a .env file."""

from functools import lru_cache
from typing import Any, Literal, Self
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import make_url

from app.crypto import decode_key
from app.sealing import Opener, Sealer

DEMO_SUFFIX = "_demo"
TTL_DAYS_MIN = 1
TTL_DAYS_MAX = 30


def empty_to_none(value: Any) -> Any:
    """Compose passes unset variables as empty strings; treat them as missing."""
    if value == "":
        return None
    return value


class Common(BaseSettings):
    """Settings every service reads."""

    # Errors never echo a value back: some settings are secrets
    model_config = SettingsConfigDict(env_file=None, extra="ignore", hide_input_in_errors=True)

    sinan_mode: Literal["demo", "live"] = "demo"
    database_url: str = "postgresql+psycopg://sinan:sinan@127.0.0.1:5432/sinan"

    def db_url(self) -> str:
        """DATABASE_URL, with `_demo` added to the database name in demo mode.

        Demo and live data never share a database, so switching modes cannot
        mix fake mail with real mail.
        """
        url = make_url(self.database_url)
        name = url.database or ""
        if self.sinan_mode == "demo" and not name.endswith(DEMO_SUFFIX):
            url = url.set(database=name + DEMO_SUFFIX)
        return url.render_as_string(hide_password=False)


class Settings(Common):
    """The API's and the Worker's settings. The Executor has its own."""

    # Key for OAuth tokens at rest. Required in live mode, unused in demo mode.
    sinan_encryption_key: SecretStr | None = None
    # Dashboard login, read by the API only (see apps/api/auth.py).
    # All three are required in live mode; demo mode falls back to demo values.
    sinan_dashboard_username: str | None = None
    sinan_dashboard_password_hash: SecretStr | None = None
    sinan_session_secret: SecretStr | None = None
    # Google OAuth client (Web application type), read by the API only.
    # Required in live mode to connect an account; demo mode uses a fake.
    google_client_id: str | None = None
    google_client_secret: SecretStr | None = None
    # The Executor's public key (ADR 0021, decision 1). The API seals write
    # and calendar tokens with it; without it, those flows stay off.
    sinan_executor_public_key: str | None = None
    # Declared only so that seeing it is an error: the private key belongs to
    # the Executor alone (see `refuse_private_key`)
    sinan_executor_private_key: SecretStr | None = None
    # Must match the redirect URI registered on the OAuth client exactly
    sinan_oauth_redirect_uri: str = "http://127.0.0.1:8000/oauth/google/callback"
    # Where the browser lands after the OAuth callback
    sinan_dashboard_url: str = "http://127.0.0.1:3000"
    # How often the Worker syncs each mailbox
    sinan_sync_interval_minutes: int = 15
    # Claude API key, read by the Worker only. Live mode without it syncs mail
    # but skips extraction; demo mode uses a fake model.
    anthropic_api_key: SecretStr | None = None
    # Extraction model (M0 item 7); M2's evaluation may change it
    sinan_llm_model: str = "claude-haiku-5-5"
    # Most emails sent to the model per Worker cycle; the rest wait (ADR 0007)
    sinan_extract_per_cycle: int = Field(default=100, ge=1)
    # Local prefilter rules with real senders; the example rules are used when
    # this file does not exist (ADR 0006)
    sinan_prefilter_rules: str = "config/prefilter.toml"
    # Local course-platform senders for the supplementary dev draw; the
    # fictional example list is used when this file does not exist (ADR 0018)
    sinan_boost_senders: str = "config/deadline_boost.toml"
    # Digest days and displayed times use this zone; storage stays UTC (ADR 0008)
    sinan_timezone: str = "America/Los_Angeles"
    # Local hour after which the Worker makes the day's digest
    sinan_digest_hour: int = Field(default=7, ge=0, le=23)
    # Days a pending proposal waits before it expires, unless its due time
    # comes sooner (ADR 0009, decision 6)
    sinan_proposal_ttl_days: int = 7

    @field_validator("sinan_proposal_ttl_days")
    @classmethod
    def ttl_in_range(cls, value: int) -> int:
        if TTL_DAYS_MIN <= value <= TTL_DAYS_MAX:
            return value
        raise ValueError(f"SINAN_PROPOSAL_TTL_DAYS must be {TTL_DAYS_MIN} to {TTL_DAYS_MAX} days")

    @field_validator("sinan_timezone")
    @classmethod
    def known_zone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(
                "SINAN_TIMEZONE must be an IANA name like America/Los_Angeles"
            ) from exc
        return value

    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.sinan_timezone)

    @field_validator(
        "sinan_encryption_key",
        "sinan_dashboard_username",
        "sinan_dashboard_password_hash",
        "sinan_session_secret",
        "google_client_id",
        "google_client_secret",
        "anthropic_api_key",
        "sinan_executor_public_key",
        "sinan_executor_private_key",
        mode="before",
    )
    @classmethod
    def empty_is_unset(cls, value: Any) -> Any:
        return empty_to_none(value)

    @field_validator("sinan_executor_public_key")
    @classmethod
    def valid_public_key(cls, value: str | None) -> str | None:
        if value is not None:
            Sealer(value)
        return value

    @model_validator(mode="after")
    def refuse_private_key(self) -> Self:
        """The API and the Worker must never hold the Executor's private key."""
        if self.sinan_executor_private_key is not None:
            raise ValueError("SINAN_EXECUTOR_PRIVATE_KEY is for the Executor only")
        return self

    @model_validator(mode="after")
    def check_live_key(self) -> Self:
        """Refuse to start in live mode without a valid encryption key."""
        if self.sinan_mode != "live":
            return self
        if self.sinan_encryption_key is None:
            raise ValueError("SINAN_ENCRYPTION_KEY is required in live mode")
        decode_key(self.sinan_encryption_key.get_secret_value())
        return self


class ExecutorSettings(Common):
    """The Executor's settings (ADR 0021, decision 1).

    It holds the private key that opens write and calendar tokens, and the
    OAuth client it refreshes them with. It has no field for the read-token
    key or the model's API key, and refuses to start if it is given the
    read-token key.
    """

    # Opens sealed tokens. Live mode without it executes nothing.
    sinan_executor_private_key: SecretStr | None = None
    google_client_id: str | None = None
    google_client_secret: SecretStr | None = None
    # Declared only so that seeing it is an error (see `refuse_read_key`)
    sinan_encryption_key: SecretStr | None = None

    @field_validator(
        "sinan_executor_private_key",
        "google_client_id",
        "google_client_secret",
        "sinan_encryption_key",
        mode="before",
    )
    @classmethod
    def empty_is_unset(cls, value: Any) -> Any:
        return empty_to_none(value)

    @field_validator("sinan_executor_private_key")
    @classmethod
    def valid_private_key(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None:
            Opener(value.get_secret_value())
        return value

    @model_validator(mode="after")
    def refuse_read_key(self) -> Self:
        """The Executor must never be able to open read tokens."""
        if self.sinan_encryption_key is not None:
            raise ValueError("SINAN_ENCRYPTION_KEY must not reach the Executor")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
