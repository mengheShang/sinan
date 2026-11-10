"""Demo and live data live in separate databases."""

import os
import uuid

import pytest
import sqlalchemy as sa
from pydantic import SecretStr

from app.config import Settings
from app.crypto import generate_key
from app.db.ensure import ensure_database

BASE = "postgresql+psycopg://sinan:pw@127.0.0.1:5432/sinan"


def test_demo_mode_uses_the_demo_database() -> None:
    settings = Settings(sinan_mode="demo", database_url=BASE)
    assert sa.make_url(settings.db_url()).database == "sinan_demo"


def test_live_mode_uses_the_named_database() -> None:
    key = SecretStr(generate_key())
    settings = Settings(sinan_mode="live", database_url=BASE, sinan_encryption_key=key)
    assert sa.make_url(settings.db_url()).database == "sinan"


def test_demo_suffix_is_not_added_twice() -> None:
    url = BASE.replace("/sinan", "/sinan_demo")
    settings = Settings(sinan_mode="demo", database_url=url)
    assert sa.make_url(settings.db_url()).database == "sinan_demo"


def test_password_survives() -> None:
    settings = Settings(sinan_mode="demo", database_url=BASE)
    assert sa.make_url(settings.db_url()).password == "pw"


def test_unsafe_names_are_refused() -> None:
    with pytest.raises(ValueError, match="database name"):
        ensure_database('postgresql+psycopg://u:p@127.0.0.1:1/bad"name')


@pytest.mark.db
def test_ensure_creates_once() -> None:
    base = os.environ.get("TEST_DATABASE_URL")
    if not base:
        pytest.skip("TEST_DATABASE_URL is not set")
    name = f"sinan_test_{uuid.uuid4().hex[:8]}_demo"
    url = sa.make_url(base).set(database=name).render_as_string(hide_password=False)
    try:
        assert ensure_database(url) is True
        assert ensure_database(url) is False
    finally:
        admin = sa.create_engine(base, isolation_level="AUTOCOMMIT")
        with admin.connect() as conn:
            conn.execute(sa.text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()
