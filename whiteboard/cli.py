"""`whiteboard` CLI (docs/CONTRACTS.md §10).

start / stop / status / scaffold / register, plus the hidden `_serve` that the
daemonised server process runs in the foreground.
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
import webbrowser
from pathlib import Path
from typing import Annotated

import typer

from whiteboard import config
from whiteboard.scaffold import scaffold as run_scaffold

app = typer.Typer(
    name="whiteboard",
    help="Local per-project visual whiteboard for planning with Claude Code.",
    no_args_is_help=True,
    add_completion=False,
    pretty_exceptions_enable=False,
)

ProjectDir = Annotated[
    Path,
    typer.Option("--project-dir", "-p", help="Project root (its state lives in <root>/.whiteboard/)."),
]

START_TIMEOUT_S = 8.0
STOP_TIMEOUT_S = 5.0
POLL_INTERVAL_S = 0.1


def _emit(payload: dict) -> None:
    typer.echo(json.dumps(payload, indent=2))


def _root(project_dir: Path) -> Path:
    root = Path(project_dir).expanduser().resolve()
    if not root.is_dir():
        _emit({"error": f"project dir does not exist: {root}"})
        raise typer.Exit(code=2)
    return root


def _serve_command(root: Path) -> list[str]:
    uv = shutil.which("uv") or "uv"
    return [
        uv, "run", "--project", str(config.AGENTBOARD_HOME), "--frozen",
        "whiteboard", "_serve", "--project-dir", str(root),
    ]


def _spawn_server(root: Path) -> subprocess.Popen:
    """Detach the server per docs/research/python-server.md: never a pipe."""
    log_path = config.server_log_path(root)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    env.pop("VIRTUAL_ENV", None)  # let `uv run --project` pick the AgentBoard venv
    with log_path.open("ab") as log:
        return subprocess.Popen(
            _serve_command(root),
            cwd=str(root),
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
            env=env,
        )


def _terminate(pid: int, timeout: float = STOP_TIMEOUT_S) -> bool:
    """SIGTERM the whole session, wait, then SIGKILL. Returns True if it died."""
    if not config.pid_alive(pid):
        return True
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(os.getpgid(pid), sig)
        except (ProcessLookupError, PermissionError):
            try:
                os.kill(pid, sig)
            except (ProcessLookupError, PermissionError):
                pass
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not config.pid_alive(pid):
                return True
            time.sleep(POLL_INTERVAL_S)
    return not config.pid_alive(pid)


def _log_tail(root: Path, lines: int = 20) -> str:
    try:
        text = config.server_log_path(root).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return "\n".join(text.splitlines()[-lines:])


@app.command()
def start(
    project_dir: ProjectDir = Path("."),
    force: Annotated[bool, typer.Option("--force", help="Restart even if a healthy server is running.")] = False,
    open_browser: Annotated[bool, typer.Option("--open/--no-open", help="Open the canvas in a browser.")] = True,
    timeout: Annotated[float, typer.Option("--timeout", hidden=True)] = START_TIMEOUT_S,
) -> None:
    """Start (or reuse) the project's whiteboard server and print server.json."""
    root = _root(project_dir)
    healthy, info = config.is_running(root)
    if healthy and info and not force:
        _emit(info)
        if open_browser:
            webbrowser.open(info["url"])
        return

    if info and config.pid_alive(info.get("pid")):
        _terminate(int(info["pid"]))
    config.remove_server_json(root)

    proc = _spawn_server(root)
    deadline = time.monotonic() + timeout
    info = None
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            _emit({"error": f"server exited with code {proc.returncode}", "log": _log_tail(root)})
            raise typer.Exit(code=1)
        info = config.read_server_json(root)
        if info and config.server_healthy(info, timeout=0.5):
            break
        time.sleep(POLL_INTERVAL_S)
    else:
        _emit({"error": f"server did not become healthy within {timeout:.0f}s", "log": _log_tail(root)})
        raise typer.Exit(code=1)

    _emit(info)
    if open_browser:
        webbrowser.open(info["url"])


@app.command()
def stop(project_dir: ProjectDir = Path(".")) -> None:
    """Stop the project's server (SIGTERM, then SIGKILL) and remove server.json."""
    root = _root(project_dir)
    info = config.read_server_json(root)
    pid = int(info.get("pid") or 0) if info else 0
    was_alive = config.pid_alive(pid)
    stopped = _terminate(pid) if was_alive else False
    config.remove_server_json(root)
    _emit({"stopped": stopped, "was_running": was_alive, "pid": pid or None})


@app.command()
def status(
    project_dir: ProjectDir = Path("."),
    as_json: Annotated[bool, typer.Option("--json/--human", help="Output format.")] = True,
) -> None:
    """Report whether the server is healthy (exit 1 if not)."""
    root = _root(project_dir)
    healthy, info = config.is_running(root)
    payload = {"running": healthy, "project": str(root), **(info or {})}
    if as_json:
        _emit(payload)
    elif healthy and info:
        typer.echo(f"whiteboard running for {root}: {info['url']} (pid {info['pid']})")
    else:
        typer.echo(f"whiteboard not running for {root}")
    if not healthy:
        raise typer.Exit(code=1)


@app.command("scaffold")
def scaffold_cmd(
    project_dir: ProjectDir = Path("."),
    force: Annotated[bool, typer.Option("--force", help="Overwrite existing template files.")] = False,
) -> None:
    """Create .whiteboard/, agent definitions, hooks and gitignore entries."""
    root = _root(project_dir)
    _emit(run_scaffold(root, force=force))


def _mcp_registered(root: Path) -> bool:
    try:
        data = json.loads((root / ".mcp.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return False
    servers = data.get("mcpServers") if isinstance(data, dict) else None
    return isinstance(servers, dict) and "whiteboard" in servers


@app.command()
def register(project_dir: ProjectDir = Path(".")) -> None:
    """Register the running server with Claude Code via `claude mcp add` (idempotent)."""
    root = _root(project_dir)
    info = config.read_server_json(root)
    if not info or not info.get("mcp_url"):
        _emit({"registered": False, "first_registration": False, "error": "server.json missing; run `whiteboard start` first"})
        raise typer.Exit(code=1)
    mcp_url = info["mcp_url"]
    first = not _mcp_registered(root)
    claude = shutil.which("claude")
    if not claude:
        _emit({"registered": False, "first_registration": first, "mcp_url": mcp_url,
               "warning": "claude CLI not found on PATH; run: "
                          f"claude mcp add --transport http --scope project whiteboard {mcp_url}"})
        return
    cmd = [claude, "mcp", "add", "--transport", "http", "--scope", "project", "whiteboard", mcp_url]
    proc = subprocess.run(cmd, cwd=str(root), capture_output=True, text=True, stdin=subprocess.DEVNULL)
    if proc.returncode != 0:
        _emit({"registered": False, "first_registration": first, "mcp_url": mcp_url,
               "error": (proc.stderr or proc.stdout).strip()[-2000:]})
        raise typer.Exit(code=1)
    _emit({"registered": True, "first_registration": first, "mcp_url": mcp_url})


@app.command("_serve", hidden=True)
def serve(project_dir: ProjectDir = Path(".")) -> None:
    """Run the server in the foreground (used by `start`)."""
    root = _root(project_dir)
    preset = os.environ.get("WHITEBOARD_PORT")
    if preset and preset.isdigit():
        port = int(preset)  # explicit request (tests, dev); run_foreground falls back if busy
    else:
        sock, port = config.bind_port(root)
        sock.close()  # run_foreground binds it again; SO_REUSEADDR makes that safe
        os.environ["WHITEBOARD_PORT"] = str(port)
    config.write_server_json(root, port)
    try:
        from whiteboard.server.app import run_foreground

        run_foreground(root)
    finally:
        info = config.read_server_json(root)
        if info and info.get("pid") == os.getpid():
            config.remove_server_json(root)


def main() -> None:
    app()


if __name__ == "__main__":
    sys.exit(main())
