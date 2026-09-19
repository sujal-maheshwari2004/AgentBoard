"""Cross-project liaison: the per-user server registry and peer lookups. See docs/CONTRACTS.md §1 and §4.

Registry file: `~/.whiteboard/registry.json` (override with env `WHITEBOARD_REGISTRY`), shaped
`{"/abs/project/path": {"port": 43217, "pid": 4242, "started_at": "..."}}`. Peers are reached over
HTTP on 127.0.0.1 with a 2 s timeout; any failure falls back to the injected file loader.
"""

from __future__ import annotations

import errno
import fcntl
import inspect
import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx2

from whiteboard.plan.model import Skeleton

REGISTRY_ENV = "WHITEBOARD_REGISTRY"
PEER_TIMEOUT_S = 2.0


def registry_path() -> Path:
    override = os.environ.get(REGISTRY_ENV)
    if override:
        return Path(override).expanduser()
    return Path.home() / ".whiteboard" / "registry.json"


def project_key(project: str | Path) -> str:
    """Canonical registry key for a project path (absolute, symlinks resolved)."""
    return str(Path(project).expanduser().resolve())


def pid_alive(pid: Any) -> bool:
    """`os.kill(pid, 0)` semantics: ESRCH -> dead, EPERM -> alive (someone else's process)."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError as exc:  # pragma: no cover - platform oddities
        return exc.errno == errno.EPERM
    return True


def _parse_registry(raw: bytes | None) -> dict:
    if not raw:
        return {}
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _prune(data: dict) -> dict:
    return {
        project: entry
        for project, entry in data.items()
        if isinstance(entry, dict) and pid_alive(entry.get("pid"))
    }


def registry_read() -> dict:
    """The registry with entries whose pid is dead removed (the file is not rewritten)."""
    path = registry_path()
    try:
        raw = path.read_bytes()
    except (FileNotFoundError, NotADirectoryError):
        return {}
    return _prune(_parse_registry(raw))


def registry_update(project: str, entry: dict | None) -> dict:
    """Read-modify-write under `fcntl.flock`: set `entry` for `project`, or remove it when None.
    Dead entries are pruned on the way. Returns the new registry contents."""
    path = registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    key = project_key(project)
    with open(path, "a+b") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            fh.seek(0)
            data = _prune(_parse_registry(fh.read()))
            if entry is None:
                data.pop(key, None)
            else:
                data[key] = dict(entry)
            tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
            tmp.write_bytes((json.dumps(data, indent=2, sort_keys=True) + "\n").encode("utf-8"))
            os.replace(tmp, path)
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    return data


def registry_lookup(project_path: str | Path) -> dict | None:
    """Live registry entry for a project, or None."""
    reg = registry_read()
    entry = reg.get(project_key(project_path))
    if entry is None:
        entry = reg.get(str(project_path))
    return entry


def peer_base_url(entry: dict) -> str | None:
    try:
        port = int(entry["port"])
    except (KeyError, TypeError, ValueError):
        return None
    return f"http://127.0.0.1:{port}"


async def _call_loader(file_loader: Callable[[Path], Any], path: Path) -> Any:
    result = file_loader(path)
    if inspect.isawaitable(result):
        result = await result
    return result


async def _peer_get_json(project_path: str, route: str) -> Any | None:
    """GET `route` on the project's live server; None on any failure."""
    entry = registry_lookup(project_path)
    if not entry:
        return None
    base = peer_base_url(entry)
    if base is None:
        return None
    try:
        async with httpx2.AsyncClient(timeout=PEER_TIMEOUT_S) as client:
            resp = await client.get(f"{base}{route}")
            if resp.status_code != 200:
                return None
            return resp.json()
    except Exception:
        return None


async def fetch_skeleton(project_path: str, *, file_loader: Callable[[Path], Skeleton]) -> Skeleton:
    """Skeleton of another project: `GET /skeleton` on its running server, else `file_loader(path)`."""
    data = await _peer_get_json(project_path, "/skeleton")
    if isinstance(data, dict):
        try:
            return Skeleton.model_validate(data)
        except Exception:
            pass
    result = await _call_loader(file_loader, Path(project_path))
    return result if isinstance(result, Skeleton) else Skeleton.model_validate(result)


async def fetch_peer_node(project_path: str, node_id: str, *, file_loader) -> dict:
    """One node of another project as a dict: `GET /api/nodes/{id}`, else the matching entry of
    `file_loader(path).nodes`. Raises KeyError when the node does not exist."""
    data = await _peer_get_json(project_path, f"/api/nodes/{node_id}")
    if isinstance(data, dict) and data.get("id") == node_id:
        return data
    skeleton = await _call_loader(file_loader, Path(project_path))
    nodes = skeleton.nodes if hasattr(skeleton, "nodes") else skeleton.get("nodes", [])
    for node in nodes:
        item = node if isinstance(node, dict) else node.model_dump()
        if item.get("id") == node_id:
            return item
    raise KeyError(f"node {node_id!r} not found in project {project_path}")


__all__ = [
    "PEER_TIMEOUT_S", "REGISTRY_ENV", "fetch_peer_node", "fetch_skeleton", "peer_base_url",
    "pid_alive", "project_key", "registry_lookup", "registry_path", "registry_read", "registry_update",
]
