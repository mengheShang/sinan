"""Runtime settings. Read from environment variables only, never from a .env file."""

from functools import lru_cache
from typing import Literal, Self

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.crypto import decode_key


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore")

    sinan_mode: Literal["demo", "live"] = "demo"
    database_url: str = "postgresql+psycopg://sinan:sinan@127.0.0.1:5432/sinan"
    # Key for OAuth tokens at rest. Required in live mode, unused in demo mode.
    sinan_encryption_key: SecretStr | None = None

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
