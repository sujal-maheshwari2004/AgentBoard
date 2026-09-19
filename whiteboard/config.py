"""Per-project runtime configuration: paths, ports, server.json, liveness.

See docs/CONTRACTS.md §1 (server.json) and docs/research/python-server.md
("Per-project isolation").
"""
from __future__ import annotations

import errno
import hashlib
import json
import os
import socket
from datetime import datetime, timezone
from pathlib import Path

PORT_BASE = 43000
PORT_SPAN = 1000
PORT_PROBES = 64
SERVER_JSON_VERSION = 1

# The AgentBoard checkout that owns the venv `whiteboard start` spawns from.
# Env override first; default = the repo dir containing this package.
AGENTBOARD_HOME: Path = Path(
    os.environ.get("AGENTBOARD_HOME") or Path(__file__).resolve().parent.parent
).expanduser()


def whiteboard_dir(root: Path) -> Path:
    return Path(root) / ".whiteboard"


def server_json_path(root: Path) -> Path:
    return whiteboard_dir(root) / "server.json"


def server_log_path(root: Path) -> Path:
    return whiteboard_dir(root) / "server.log"


def project_name(root: Path) -> str:
    return Path(root).resolve().name


def preferred_port(root: Path) -> int:
    """Deterministic per-project port (blake2b, never salted `hash()`)."""
    key = str(Path(root).resolve()).encode("utf-8")
    digest = hashlib.blake2b(key, digest_size=8).digest()
    return PORT_BASE + int.from_bytes(digest, "big") % PORT_SPAN


def _try_bind(port: int) -> socket.socket | None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind(("127.0.0.1", port))
    except OSError:
        sock.close()
        return None
    return sock


def bind_port(root: Path) -> tuple[socket.socket, int]:
    """Bind a listening-ready socket: preferred port, probe 64 upward, else OS-assigned.

    The caller owns the returned socket (close it, or hand it to uvicorn).
    """
    start = preferred_port(root)
    for offset in range(PORT_PROBES):
        sock = _try_bind(start + offset)
        if sock is not None:
            return sock, sock.getsockname()[1]
    sock = _try_bind(0)
    if sock is None:  # pragma: no cover - only if the loopback is unusable
        raise OSError("could not bind any loopback port")
    return sock, sock.getsockname()[1]


def read_server_json(root: Path) -> dict | None:
    path = server_json_path(root)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError:
        return None
    try:
        info = json.loads(text)
    except ValueError:
        return None
    return info if isinstance(info, dict) else None


def make_server_info(root: Path, port: int, *, pid: int | None = None) -> dict:
    return {
        "pid": os.getpid() if pid is None else pid,
        "port": port,
        "started_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "project": str(Path(root).resolve()),
        "url": f"http://127.0.0.1:{port}",
        "mcp_url": f"http://127.0.0.1:{port}/mcp",
        "version": SERVER_JSON_VERSION,
    }


def write_server_json(root: Path, port: int, *, pid: int | None = None) -> dict:
    """Atomically write `.whiteboard/server.json`; returns the dict written."""
    info = make_server_info(root, port, pid=pid)
    path = server_json_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    data = (json.dumps(info, indent=2) + "\n").encode("utf-8")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)
    return info


def remove_server_json(root: Path) -> None:
    try:
        server_json_path(root).unlink()
    except FileNotFoundError:
        pass


def pid_alive(pid: int | None) -> bool:
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError as exc:
        if exc.errno == errno.EPERM:
            return True
        return False
    return True


def server_healthy(info: dict | None, timeout: float = 1.0) -> bool:
    """True when `GET /api/health` answers and its `project` matches server.json."""
    if not info:
        return False
    url = info.get("url") or (f"http://127.0.0.1:{info['port']}" if info.get("port") else None)
    if not url:
        return False
    try:
        import httpx2

        resp = httpx2.get(f"{url}/api/health", timeout=timeout)
        if resp.status_code != 200:
            return False
        body = resp.json()
    except Exception:
        return False
    if not isinstance(body, dict):
        return False
    expected = info.get("project")
    if expected is None:
        return True
    return _same_path(body.get("project"), expected)


def _same_path(a: object, b: object) -> bool:
    if not isinstance(a, str) or not isinstance(b, str):
        return False
    if a == b:
        return True
    try:
        return Path(a).resolve() == Path(b).resolve()
    except OSError:
        return False


def is_running(root: Path, timeout: float = 1.0) -> tuple[bool, dict | None]:
    """(healthy, info) — healthy requires a live pid AND a matching /api/health."""
    info = read_server_json(root)
    if not info:
        return False, None
    if not pid_alive(info.get("pid")):
        return False, info
    return server_healthy(info, timeout=timeout), info
