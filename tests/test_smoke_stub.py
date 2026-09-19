from pathlib import Path

from starlette.testclient import TestClient

from whiteboard.server.app import create_app


def test_stub_health(tmp_path: Path) -> None:
    with TestClient(create_app(tmp_path)) as c:
        r = c.get("/api/health")
        assert r.status_code == 200
        assert r.json()["project"] == str(tmp_path.resolve())
