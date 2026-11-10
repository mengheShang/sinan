"""Create the configured database if it does not exist yet.

Compose's PostgreSQL image creates only POSTGRES_DB on first start. Demo mode
uses a second database (`<name>_demo`, see Settings.db_url), so the API runs
this before migrating. Run it with `python -m app.db.ensure`.
"""

import logging
import re

import sqlalchemy as sa

from app.config import get_settings

log = logging.getLogger("sinan.db")

# Only plain names, so the CREATE DATABASE statement cannot be injected into
SAFE_NAME = re.compile(r"^[a-z][a-z0-9_]{0,62}$")


def ensure_database(url: str) -> bool:
    """Create the database in `url` if missing. Returns True if it was created."""
    target = sa.make_url(url)
    name = target.database or ""
    if not SAFE_NAME.match(name):
        raise ValueError("database name must be lowercase letters, digits and underscores")
    admin_url = target.set(database="postgres")
    engine = sa.create_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as conn:
            query = sa.text("SELECT 1 FROM pg_database WHERE datname = :name")
            exists = conn.execute(query, {"name": name}).first() is not None
            if exists:
                return False
            conn.execute(sa.text(f'CREATE DATABASE "{name}"'))
    finally:
        engine.dispose()
    return True


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    settings = get_settings()
    url = settings.db_url()
    name = sa.make_url(url).database
    if ensure_database(url):
        log.info("Created database %s", name)
    else:
        log.info("Database %s already exists", name)


if __name__ == "__main__":
    main()
