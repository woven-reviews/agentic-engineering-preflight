from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_health_check() -> None:
    r = client.get("/api/v1/utils/health-check/")
    assert r.status_code == 200
    assert r.json() is True
