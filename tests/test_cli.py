import json
import os
import shutil
import stat
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from whiteboard import config
from whiteboard.cli import app

runner = CliRunner()


def _run(*args: str, env: dict | None = None):
    return runner.invoke(app, list(args), env=env, catch_exceptions=False)


def _json(result) -> dict:
    return json.loads(result.stdout)


def test_scaffold_command(tmp_path: Path) -> None:
    result = _run("scaffold", "--project-dir", str(tmp_path))
    assert result.exit_code == 0, result.output
    payload = _json(result)
    assert ".whiteboard/COLLABORATION.md" in payload["created"]
    assert (tmp_path / ".whiteboard" / "PLAN.md").exists()
    again = _json(_run("scaffold", "--project-dir", str(tmp_path)))
    assert again["created"] == []


def test_status_when_not_running(tmp_path: Path) -> None:
    result = _run("status", "--project-dir", str(tmp_path))
    assert result.exit_code == 1
    payload = _json(result)
    assert payload["running"] is False
    human = _run("status", "--project-dir", str(tmp_path), "--human")
    assert human.exit_code == 1
    assert "not running" in human.stdout


def test_status_with_stale_server_json(tmp_path: Path) -> None:
    config.write_server_json(tmp_path, 1, pid=2_000_000_000)
    result = _run("status", "--project-dir", str(tmp_path))
    assert result.exit_code == 1
    assert _json(result)["running"] is False
    assert _json(result)["pid"] == 2_000_000_000


def test_missing_project_dir_exits_2(tmp_path: Path) -> None:
    result = _run("status", "--project-dir", str(tmp_path / "nope"))
    assert result.exit_code == 2


def test_stop_when_not_running(tmp_path: Path) -> None:
    config.write_server_json(tmp_path, 1, pid=2_000_000_000)
    result = _run("stop", "--project-dir", str(tmp_path))
    assert result.exit_code == 0
    assert _json(result) == {"stopped": False, "was_running": False, "pid": 2_000_000_000}
    assert not config.server_json_path(tmp_path).exists()


def _fake_claude(bin_dir: Path, *, exit_code: int = 0) -> Path:
    bin_dir.mkdir(parents=True, exist_ok=True)
    log = bin_dir / "claude.argv"
    script = bin_dir / "claude"
    script.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' \"$@\" >> '{log}'\n"
        f"exit {exit_code}\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return log


def test_register_without_server_json(tmp_path: Path) -> None:
    result = _run("register", "--project-dir", str(tmp_path))
    assert result.exit_code == 1
    assert _json(result)["registered"] is False


def test_register_with_fake_claude(tmp_path: Path, monkeypatch) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    config.write_server_json(project, 43555, pid=os.getpid())
    log = _fake_claude(tmp_path / "bin")
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}")

    result = _run("register", "--project-dir", str(project))
    assert result.exit_code == 0, result.output
    payload = _json(result)
    assert payload == {"registered": True, "first_registration": True, "mcp_url": "http://127.0.0.1:43555/mcp"}
    argv = log.read_text().splitlines()
    assert argv == ["mcp", "add", "--transport", "http", "--scope", "project", "whiteboard", "http://127.0.0.1:43555/mcp"]

    # Second run: .mcp.json now says whiteboard is registered -> not first.
    (project / ".mcp.json").write_text(json.dumps({"mcpServers": {"whiteboard": {"type": "http", "url": payload["mcp_url"]}}}))
    payload2 = _json(_run("register", "--project-dir", str(project)))
    assert payload2["registered"] is True and payload2["first_registration"] is False
    assert len(log.read_text().splitlines()) == 16  # invoked again (idempotent re-add)


def test_register_when_claude_fails(tmp_path: Path, monkeypatch) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    config.write_server_json(project, 43556, pid=os.getpid())
    _fake_claude(tmp_path / "bin", exit_code=3)
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}")
    result = _run("register", "--project-dir", str(project))
    assert result.exit_code == 1
    assert _json(result)["registered"] is False


def test_register_when_claude_missing(tmp_path: Path, monkeypatch) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    config.write_server_json(project, 43557, pid=os.getpid())
    empty_bin = tmp_path / "emptybin"
    empty_bin.mkdir()
    monkeypatch.setenv("PATH", str(empty_bin))
    monkeypatch.setattr(shutil, "which", lambda name, *a, **k: None)
    result = _run("register", "--project-dir", str(project))
    assert result.exit_code == 0
    payload = _json(result)
    assert payload["registered"] is False and payload["first_registration"] is True
    assert "claude mcp add" in payload["warning"]


@pytest.mark.slow
@pytest.mark.skipif(shutil.which("uv") is None, reason="uv not on PATH")
def test_start_status_stop_end_to_end(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    try:
        result = _run("start", "--project-dir", str(project), "--no-open", "--timeout", "30")
        assert result.exit_code == 0, result.output
        info = _json(result)
        assert info["project"] == str(project.resolve())
        assert info["url"] == f"http://127.0.0.1:{info['port']}"
        assert info["mcp_url"].endswith("/mcp")
        assert config.pid_alive(info["pid"])
        assert config.server_healthy(info)
        assert config.server_log_path(project).exists()

        # A second start reuses the healthy server.
        again = _json(_run("start", "--project-dir", str(project), "--no-open"))
        assert again["pid"] == info["pid"] and again["port"] == info["port"]

        status = _run("status", "--project-dir", str(project))
        assert status.exit_code == 0
        assert _json(status)["running"] is True

        # --force restarts with a new pid.
        forced = _json(_run("start", "--project-dir", str(project), "--no-open", "--force", "--timeout", "30"))
        assert forced["pid"] != info["pid"]
        assert not config.pid_alive(info["pid"])
        info = forced
    finally:
        stopped = _run("stop", "--project-dir", str(project))
    assert stopped.exit_code == 0
    payload = _json(stopped)
    assert payload["was_running"] is True and payload["stopped"] is True
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and config.pid_alive(info["pid"]):
        time.sleep(0.1)
    assert not config.pid_alive(info["pid"])
    assert not config.server_json_path(project).exists()
    assert _run("status", "--project-dir", str(project)).exit_code == 1
