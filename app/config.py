"""Runtime settings. Read from environment variables only, never from a .env file."""

from functools import lru_cache
from typing import Any, Literal, Self

from pydantic import SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.crypto import decode_key


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore")

    sinan_mode: Literal["demo", "live"] = "demo"
    database_url: str = "postgresql+psycopg://sinan:sinan@127.0.0.1:5432/sinan"
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
    # Must match the redirect URI registered on the OAuth client exactly
    sinan_oauth_redirect_uri: str = "http://127.0.0.1:8000/oauth/google/callback"
    # Where the browser lands after the OAuth callback
    sinan_dashboard_url: str = "http://127.0.0.1:3000"

    @field_validator(
        "sinan_encryption_key",
        "sinan_dashboard_username",
        "sinan_dashboard_password_hash",
        "sinan_session_secret",
        "google_client_id",
        "google_client_secret",
        mode="before",
    )
    @classmethod
    def empty_is_unset(cls, value: Any) -> Any:
        """Compose passes unset variables as empty strings; treat them as missing."""
        if value == "":
            return None
        return value

    @model_validator(mode="after")
    def check_live_key(self) -> Self:
        """Refuse to start in live mode without a valid encryption key."""
        if self.sinan_mode != "live":
            return self
        if self.sinan_encryption_key is None:
            raise ValueError("SINAN_ENCRYPTION_KEY is required in live mode")
        decode_key(self.sinan_encryption_key.get_secret_value())
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
