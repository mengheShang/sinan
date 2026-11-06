import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from app.db.session import check_db
from apps.api.main import app

client = TestClient(app)


def test_health_ok() -> None:
    resp = client.get("/health")
    body = resp.json()
    assert resp.status_code == 200
    assert body["status"] == "ok"
    assert body["mode"] == "demo"


@pytest.mark.db
def test_check_db() -> None:
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is not set")
    engine = create_engine(url)
    assert check_db(engine)
