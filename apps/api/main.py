"""FastAPI entry point.

Every route requires a signed-in session unless it is marked with
@auth.public, so a new route is protected without extra code.
"""

from fastapi import Depends, FastAPI, Response, status
from sqlalchemy.exc import SQLAlchemyError

from app.config import get_settings
from app.db.session import check_db, get_engine
from app.logredact import install_redaction
from apps.api import auth, digest, oauth

# Uvicorn sets up its log handlers before importing this module
install_redaction()


def create_app() -> FastAPI:
    settings = get_settings()
    # Fail at startup, not at first login, when live mode lacks login settings
    auth.get_auth()
    # Same for the OAuth client in live mode
    oauth.get_connector()
    # Interactive docs only in demo mode
    demo = settings.sinan_mode == "demo"
    api = FastAPI(
        title="Sinan API",
        version="0.1.0",
        dependencies=[Depends(auth.require_session)],
        docs_url="/docs" if demo else None,
        redoc_url=None,
        openapi_url="/openapi.json" if demo else None,
    )
    api.include_router(auth.router)
    api.include_router(oauth.router)
    api.include_router(digest.router)
    api.add_api_route("/health", health)
    api.add_api_route("/health/db", health_db)
    return api


@auth.public
def health() -> dict[str, str]:
    """Liveness check. Does not touch the database."""
    settings = get_settings()
    return {"status": "ok", "mode": settings.sinan_mode}


@auth.public
def health_db(response: Response) -> dict[str, str]:
    """Database connectivity check."""
    engine = get_engine()
    try:
        ok = check_db(engine)
    except SQLAlchemyError:
        ok = False
    if not ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "error"}
    return {"status": "ok"}


app = create_app()
