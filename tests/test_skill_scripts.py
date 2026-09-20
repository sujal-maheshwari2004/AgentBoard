import json
import os
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SKILL = REPO / "skill" / "whiteboard"
SCRIPTS = sorted((SKILL / "scripts").glob("*.sh"))

ENTER_JSON_KEYS = {
    "url", "mcp_url", "port", "first_registration", "scaffolded", "project", "session_project",
    "session_project_matches", "inbound_ok", "inbound_scope", "inbound_sources",
    "agents_installed", "warnings",
}


def test_skill_frontmatter() -> None:
    text = (SKILL / "SKILL.md").read_text()
    assert text.startswith("---\nname: whiteboard\n")
    assert "allowed-tools: Bash(bash ~/.claude/skills/whiteboard/scripts/*) Read" in text
    assert 'Use when the user says "enter whiteboard mode"' in text
    assert 'bash ~/.claude/skills/whiteboard/scripts/enter.sh "$PWD"' in text
    assert "/mcp" in text and "whiteboard-task" in text and "whiteboard-liaison" in text
    assert len(text.splitlines()) <= 160


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
    assert "install-agents:" in text
    assert "install-skill: install-agents" in text
    assert "python -m whiteboard.agents_install" in text
    # the skill dir stays a symlink; the agent definitions are copies
    assert "ln -s" in text


# --- SKILL.md v2 protocol -----------------------------------------------------------------


def _skill_text() -> str:
    return (SKILL / "SKILL.md").read_text()


def test_skill_documents_the_v2_enter_json_and_fallback() -> None:
    text = _skill_text()
    # `inbound_sources` is diagnostic detail the skill does not need to act on; every other key
    # the script emits must be named.
    for key in (ENTER_JSON_KEYS - {"inbound_sources"}) | {"fix_hint"}:
        assert key in text, key
    assert "general-purpose" in text
    assert "agents_installed.ok" in text
    assert "session_project_matches" in text and "inbound_ok" in text


def test_skill_forbids_writing_diagrams_and_user_settings() -> None:
    text = _skill_text()
    assert "Never call `write_diagram`" in text
    assert "`write_diagram` is not yours to call" in text
    assert "Never edit `~/.claude/settings.json` yourself" in text
    assert "fix_hint" in text
    assert "propose_diagram" in text  # root proposes; the human approves every board


def test_skill_sendmessage_allowlist_has_exactly_four_uses() -> None:
    text = _skill_text()
    rule = next(line for line in text.splitlines() if "allowed for exactly four things" in line)
    for use in ("forwarded chat", "prompt replies", "plan-edit notices", "diagram-edit notices"):
        assert use in rule, use
    assert "Nothing else, ever." in rule
    # exactly three concrete call sites: forwarded chat, prompt reply, plan-edit notice
    # (the diagram-edit row reuses the plan-edit one)
    assert text.count("SendMessage(to=") == 3


def test_skill_dispatch_records_and_finalizes() -> None:
    text = _skill_text()
    dispatch = text[text.index("\n## Dispatch\n"):text.index("\n### `general-purpose` fallback")]
    assert dispatch.index("propose_dispatch") < dispatch.index("set_agent_ref") < dispatch.index("finalize_agent")
    assert "Never spawn before approval." in text
    assert "Without it you have lost the agent." in text


# --- enter.sh v2 --------------------------------------------------------------------------


ENTER = SKILL / "scripts" / "enter.sh"


def _enter(project: Path, home: Path, env_extra: dict | None = None) -> subprocess.CompletedProcess:
    env = {**os.environ, "AGENTBOARD_HOME": str(REPO), "HOME": str(home)}
    env.pop("CLAUDE_PROJECT_DIR", None)
    env.pop("CLAUDE_CODE_MESSAGING_SOCKET", None)
    env.update(env_extra or {})
    return subprocess.run(
        ["bash", str(ENTER), str(project), "--report-only"],
        capture_output=True, text=True, env=env, cwd=str(project), timeout=120,
    )


def test_enter_sh_checks_session_project_and_inbound() -> None:
    text = ENTER.read_text()
    for token in ("CLAUDE_PROJECT_DIR", "session_project_matches", "crossSessionInbound",
                  "inbound_scope", "inbound_sources", "install_agent_definitions", "fix_hint"):
        assert token in text, token
    assert "NEVER writes ~/.claude/settings.json" in text
    # the only settings.json mutation in the whole script is inside the fix_hint string, which
    # the OWNER runs. Nothing outside it writes a settings file.
    hint_start = text.index('out["fix_hint"]')
    hint_end = text.index("if register.get(", hint_start)
    outside = text[:hint_start] + text[hint_end:]
    assert "write_text" not in outside
    assert "settings.json.bak" not in outside


def test_enter_sh_json_keys(tmp_path: Path) -> None:
    home, project = tmp_path / "home", tmp_path / "proj"
    home.mkdir()
    project.mkdir()
    proc = _enter(project, home)
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert ENTER_JSON_KEYS <= set(payload), ENTER_JSON_KEYS - set(payload)
    assert payload["project"] == str(project.resolve())
    assert payload["session_project"] == str(project.resolve())
    assert payload["session_project_matches"] is True
    assert payload["agents_installed"]["ok"] is True
    assert (home / ".claude" / "agents" / "whiteboard-task.md").is_file()
    assert isinstance(payload["warnings"], list)


def test_enter_sh_reports_mismatched_session_project(tmp_path: Path) -> None:
    home, project, session = tmp_path / "home", tmp_path / "proj", tmp_path / "elsewhere"
    for d in (home, project, session):
        d.mkdir()
    payload = json.loads(_enter(project, home, {"CLAUDE_PROJECT_DIR": str(session)}).stdout)
    assert payload["session_project_matches"] is False
    assert payload["session_project"] == str(session.resolve())
    assert payload["project"] == str(project.resolve())
    assert any(str(session.resolve()) in w for w in payload["warnings"])
    assert str(project.resolve() / ".claude" / "settings.local.json") in payload["inbound_sources"]


def test_enter_sh_inbound_ok_from_user_scope(tmp_path: Path) -> None:
    home, project = tmp_path / "home", tmp_path / "proj"
    (home / ".claude").mkdir(parents=True)
    project.mkdir()
    (home / ".claude" / "settings.json").write_text(json.dumps({"crossSessionInbound": "accept"}))
    payload = json.loads(_enter(project, home).stdout)
    assert payload["inbound_ok"] is True
    assert payload["inbound_scope"] == "user"
    assert "fix_hint" not in payload


def test_enter_sh_ignores_the_whiteboarded_projects_setting(tmp_path: Path) -> None:
    home, project, session = tmp_path / "home", tmp_path / "proj", tmp_path / "elsewhere"
    home.mkdir()
    session.mkdir()
    (project / ".claude").mkdir(parents=True)
    (project / ".claude" / "settings.local.json").write_text(json.dumps({"crossSessionInbound": "accept"}))
    payload = json.loads(_enter(project, home, {"CLAUDE_PROJECT_DIR": str(session)}).stdout)
    assert payload["inbound_ok"] is False
    assert payload["inbound_scope"] is None
    assert payload["inbound_sources"][str(project.resolve() / ".claude" / "settings.local.json")] == "accept"


def test_enter_sh_inbound_ok_from_the_session_project(tmp_path: Path) -> None:
    home, project, session = tmp_path / "home", tmp_path / "proj", tmp_path / "elsewhere"
    home.mkdir()
    project.mkdir()
    (session / ".claude").mkdir(parents=True)
    (session / ".claude" / "settings.local.json").write_text(json.dumps({"crossSessionInbound": "accept"}))
    payload = json.loads(_enter(project, home, {"CLAUDE_PROJECT_DIR": str(session)}).stdout)
    assert payload["inbound_ok"] is True
    assert payload["inbound_scope"] == "session-project"


def test_enter_sh_emits_fix_hint_when_inbound_missing(tmp_path: Path) -> None:
    home, project = tmp_path / "home", tmp_path / "proj"
    home.mkdir()
    project.mkdir()
    payload = json.loads(_enter(project, home).stdout)
    assert payload["inbound_ok"] is False
    hint = payload["fix_hint"]
    assert "\n" not in hint
    assert "crossSessionInbound" in hint and "settings.json.bak" in hint
    script = tmp_path / "hint.sh"
    script.write_text(hint + "\n")
    subprocess.run(["bash", "-n", str(script)], check=True, timeout=30)
    # the script itself must never create the user's settings file
    assert not (home / ".claude" / "settings.json").exists()
    assert any("crossSessionInbound" in w for w in payload["warnings"])


# --- session_hook.sh prompt mode ----------------------------------------------------------


class _FakeServer:
    """A tiny JSON server; a path that is not in `routes` answers 404 (a pre-A.S4 server)."""

    def __init__(self, routes: dict):
        self.routes = routes
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                body = outer.routes.get(self.path.split("?")[0])
                if body is None:
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                raw = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_POST(self):  # noqa: N802
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *args):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)


def _write_server_json(root: Path, url: str) -> None:
    (root / ".whiteboard").mkdir(parents=True, exist_ok=True)
    (root / ".whiteboard" / "server.json").write_text(
        json.dumps({"pid": 1, "url": url, "mcp_url": url + "/mcp"})
    )


def test_session_hook_prompt_reports_chat_and_pending_diagrams(tmp_path: Path) -> None:
    routes = {
        "/api/health": {"nodes": 3, "latest_seq": 12, "bridge": {"last_pushed_seq": 8}, "pending_diagrams": 2},
        "/api/threads": {"threads": {
            "root": {"count": 4, "last_seq": 12},
            "agent-parser": {"count": 3, "last_seq": 11},
            "agent-quiet": {"count": 1, "last_seq": 2},
        }},
        "/api/threads/root": {"thread": "root", "messages": [{"id": "chat-11"}, {"id": "chat-12"}]},
        "/api/threads/agent-parser": {"thread": "agent-parser", "messages": [{"id": "chat-10"}]},
    }
    with _FakeServer(routes) as server:
        _write_server_json(tmp_path, server.url)
        proc = _hook("prompt", tmp_path)
    assert proc.returncode == 0
    ctx = json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"]
    assert "4 unread canvas event(s)" in ctx
    assert "get_events(since_seq=8)" in ctx
    assert "Unread chat by thread:" in ctx and "root=2" in ctx and "agent-parser=1" in ctx
    assert "agent-quiet" not in ctx  # nothing new since last_pushed_seq
    assert "chat_reply" in ctx
    assert "2 diagram proposal(s) still awaiting approval" in ctx


def test_session_hook_prompt_survives_missing_threads_endpoint(tmp_path: Path) -> None:
    routes = {"/api/health": {"nodes": 1, "latest_seq": 5, "bridge": {"last_pushed_seq": 4}}}
    with _FakeServer(routes) as server:
        _write_server_json(tmp_path, server.url)
        proc = _hook("prompt", tmp_path)
    assert proc.returncode == 0
    ctx = json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"]
    assert ctx == (
        "Whiteboard: 1 unread canvas event(s); "
        "call mcp__whiteboard__get_events(since_seq=4) and act on them."
    )


def test_session_hook_prompt_accepts_pending_diagrams_as_a_list(tmp_path: Path) -> None:
    routes = {"/api/health": {"latest_seq": 3, "bridge": {"last_pushed_seq": 3},
                              "pending_diagrams": [{"request_id": "a"}, {"request_id": "b"}]}}
    with _FakeServer(routes) as server:
        _write_server_json(tmp_path, server.url)
        proc = _hook("prompt", tmp_path)
    assert proc.returncode == 0
    ctx = json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"]
    assert "2 diagram proposal(s) still awaiting approval" in ctx
    assert "unread canvas event" not in ctx


def test_session_hook_start_mentions_pending_diagrams(tmp_path: Path) -> None:
    routes = {"/api/health": {"nodes": 2, "latest_seq": 7, "bridge": {}, "pending_diagrams": 1}}
    with _FakeServer(routes) as server:
        _write_server_json(tmp_path, server.url)
        proc = _hook("start", tmp_path)
    assert proc.returncode == 0
    ctx = json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"]
    assert "1 diagram proposal(s) are still awaiting the human's approval" in ctx
