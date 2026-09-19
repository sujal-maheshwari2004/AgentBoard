"""The server boots on a bare directory (no scaffold): the Stage 0 smoke check, kept
as the "empty project" case now that T7's real app replaced the stub."""

from pathlib import Path

from starlette.testclient import TestClient

from whiteboard.server.app import create_app


def test_bare_directory_boots_and_reports_health(tmp_path: Path) -> None:
    with TestClient(create_app(tmp_path)) as c:
        r = c.get("/api/health")
        assert r.status_code == 200
        body = r.json()
        assert body["project"] == str(tmp_path.resolve())
        assert body["nodes"] == 0 and body["latest_seq"] == 0 and body["clients"] == 0
        assert c.get("/skeleton").json() == {"project": tmp_path.resolve().name, "nodes": []}
    # The lifespan created the state dir, the generated files and the log.
    wb = tmp_path / ".whiteboard"
    assert (wb / "PLAN.md").exists() and (wb / "plan" / "hld.md").exists()
    assert (wb / "server.log").exists() and (wb / "plan" / "nodes").is_dir()
