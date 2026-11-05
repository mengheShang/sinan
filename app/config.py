"""运行配置。只从环境变量读取，不直接读取 .env 文件。"""

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore")

    sinan_mode: Literal["demo", "live"] = "demo"
    database_url: str = "postgresql+psycopg://sinan:sinan@127.0.0.1:5432/sinan"


@lru_cache
def get_settings() -> Settings:
    return Settings()
