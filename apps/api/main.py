"""FastAPI entry point. M0 only provides health checks."""

from fastapi import FastAPI, Response, status
from sqlalchemy.exc import SQLAlchemyError

from app.config import get_settings
from app.db.session import check_db, get_engine

app = FastAPI(title="Sinan API", version="0.1.0")


@app.get("/health")
def health() -> dict[str, str]:
    """Liveness check. Does not touch the database."""
    settings = get_settings()
    return {"status": "ok", "mode": settings.sinan_mode}


@app.get("/health/db")
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
