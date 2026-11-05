"""FastAPI 入口。M0 只提供健康检查。"""

from fastapi import FastAPI, Response, status
from sqlalchemy.exc import SQLAlchemyError

from app.config import get_settings
from app.db.session import check_db, get_engine

app = FastAPI(title="Sinan API", version="0.1.0")


@app.get("/health")
def health() -> dict[str, str]:
    """进程存活检查，不访问数据库。"""
    settings = get_settings()
    return {"status": "ok", "mode": settings.sinan_mode}


@app.get("/health/db")
def health_db(response: Response) -> dict[str, str]:
    """数据库连通性检查。"""
    engine = get_engine()
    try:
        ok = check_db(engine)
    except SQLAlchemyError:
        ok = False
    if not ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "error"}
    return {"status": "ok"}
