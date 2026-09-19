import json
import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SKILL = REPO / "skill" / "whiteboard"
SCRIPTS = sorted((SKILL / "scripts").glob("*.sh"))


def test_skill_frontmatter() -> None:
    text = (SKILL / "SKILL.md").read_text()
    assert text.startswith("---\nname: whiteboard\n")
    assert "allowed-tools: Bash(bash ~/.claude/skills/whiteboard/scripts/*) Read" in text
    assert 'Use when the user says "enter whiteboard mode"' in text
    assert 'bash ~/.claude/skills/whiteboard/scripts/enter.sh "$PWD"' in text
    assert "/mcp" in text and "whiteboard-task" in text and "whiteboard-liaison" in text
    assert len(text.splitlines()) <= 130


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_scripts_are_executable_and_parse(script: Path) -> None:
    assert os.access(script, os.X_OK), f"{script.name} is not executable"
    assert script.read_text().startswith("#!/usr/bin/env bash")
    subprocess.run(["bash", "-n", str(script)], check=True)


def test_expected_scripts_exist() -> None:
    names = {p.name for p in SCRIPTS}
    assert {"enter.sh", "session_hook.sh"} <= names


def _hook(mode: str, cwd: Path, env_extra: dict | None = None) -> subprocess.CompletedProcess:
    env = {**os.environ, "AGENTBOARD_HOME": str(REPO), **(env_extra or {})}
    return subprocess.run(
        ["bash", str(SKILL / "scripts" / "session_hook.sh"), mode],
        input=json.dumps({"session_id": "s", "hook_event_name": "SessionStart", "cwd": str(cwd)}),
        capture_output=True, text=True, env=env, cwd=str(cwd), timeout=30,
    )


def test_session_hook_start_without_server_prints_valid_json(tmp_path: Path) -> None:
    proc = _hook("start", tmp_path)
    assert proc.returncode == 0
    payload = json.loads(proc.stdout)
    out = payload["hookSpecificOutput"]
    assert out["hookEventName"] == "SessionStart"
    assert "/whiteboard" in out["additionalContext"]


def test_session_hook_start_with_dead_server_json(tmp_path: Path) -> None:
    (tmp_path / ".whiteboard").mkdir()
    (tmp_path / ".whiteboard" / "server.json").write_text(json.dumps({"pid": 2_000_000_000, "url": "http://127.0.0.1:1"}))
    proc = _hook("start", tmp_path)
    assert proc.returncode == 0
    ctx = json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"]
    assert "not responding" in ctx


def test_session_hook_prompt_without_server_prints_nothing(tmp_path: Path) -> None:
    proc = _hook("prompt", tmp_path)
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""


def test_session_hook_tolerates_bad_stdin(tmp_path: Path) -> None:
    env = {**os.environ, "AGENTBOARD_HOME": str(REPO)}
    proc = subprocess.run(
        ["bash", str(SKILL / "scripts" / "session_hook.sh"), "start"],
        input="not json", capture_output=True, text=True, env=env, cwd=str(tmp_path), timeout=30,
    )
    assert proc.returncode == 0
    json.loads(proc.stdout)


def test_makefile_targets() -> None:
    text = (REPO / "Makefile").read_text()
    assert "install-skill:" in text and "test:" in text
