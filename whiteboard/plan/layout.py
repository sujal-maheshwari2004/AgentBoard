"""Layout sidecar (`plan/<diagram>.layout.json`). See docs/CONTRACTS.md §1 and §4."""

from __future__ import annotations

import copy
import json
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

LAYOUT_VERSION = 1
LAYOUT_MAPS: tuple[str, ...] = ("frames", "nodes", "agents", "edges")
EDGE_KEY_SEP = "__"


def default_layout(direction: str = "TD") -> dict:
    return {
        "version": LAYOUT_VERSION,
        "direction": direction,
        "updatedAt": None,
        "frames": {},
        "nodes": {},
        "agents": {},
        "edges": {},
    }


def _now_iso() -> str:
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def _normalize(layout: dict | None) -> dict:
    out = default_layout()
    if isinstance(layout, dict):
        for k, v in layout.items():
            if k in LAYOUT_MAPS:
                out[k] = dict(v) if isinstance(v, dict) else {}
            else:
                out[k] = v
    out["version"] = LAYOUT_VERSION
    if not isinstance(out.get("direction"), str) or not out["direction"]:
        out["direction"] = "TD"
    return out


def read_layout(path: Path) -> dict:
    """Parsed sidecar with every map present; the default skeleton when missing or invalid."""
    try:
        raw = Path(path).read_bytes()
    except (FileNotFoundError, NotADirectoryError):
        return default_layout()
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return default_layout()
    return _normalize(data)


def _default_writer(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def write_layout(
    path: Path, layout: dict, *, writer: Callable[[Path, bytes], None] | None = None
) -> None:
    """Write the sidecar (sets `updatedAt`). `writer` defaults to plain `Path.write_bytes`;
    the store injects `whiteboard.files.atomic.atomic_write` for atomicity."""
    out = _normalize(layout)
    out["updatedAt"] = _now_iso()
    layout["updatedAt"] = out["updatedAt"]
    data = (json.dumps(out, indent=2, sort_keys=False) + "\n").encode("utf-8")
    (writer or _default_writer)(Path(path), data)


def _merge_entry(base: object, patch: object) -> object:
    if isinstance(base, dict) and isinstance(patch, dict):
        out = dict(base)
        for k, v in patch.items():
            if v is None:
                out.pop(k, None)
            else:
                out[k] = _merge_entry(out.get(k), v)
        return out
    return copy.deepcopy(patch)


def merge_layout(current: dict, patch: dict) -> dict:
    """Deep-merge `patch` into a copy of `current` over the four maps; a `None` value deletes
    the key at that level. Top-level scalars (`direction`) are overridden when present."""
    out = copy.deepcopy(_normalize(current))
    for key, val in (patch or {}).items():
        if key in LAYOUT_MAPS:
            if val is None:
                out[key] = {}
                continue
            if not isinstance(val, dict):
                continue
            target = out[key]
            for entry_id, entry in val.items():
                if entry is None:
                    target.pop(entry_id, None)
                else:
                    target[entry_id] = _merge_entry(target.get(entry_id), entry)
        elif key in ("version", "updatedAt"):
            continue
        else:
            out[key] = copy.deepcopy(val)
    return out


def gc_layout(layout: dict, node_ids: set[str], agent_ids: set[str]) -> dict:
    """Drop node/agent entries (and edges) that reference ids no longer in the plan."""
    out = copy.deepcopy(_normalize(layout))
    out["nodes"] = {k: v for k, v in out["nodes"].items() if k in node_ids}
    out["agents"] = {k: v for k, v in out["agents"].items() if k in agent_ids}
    kept_edges: dict = {}
    for key, v in out["edges"].items():
        src, sep, dst = key.partition(EDGE_KEY_SEP)
        if sep and src in node_ids and dst in node_ids:
            kept_edges[key] = v
    out["edges"] = kept_edges
    return out


def edge_key(src: str, dst: str) -> str:
    return f"{src}{EDGE_KEY_SEP}{dst}"


__all__ = [
    "EDGE_KEY_SEP", "LAYOUT_MAPS", "LAYOUT_VERSION", "default_layout", "edge_key",
    "gc_layout", "merge_layout", "read_layout", "write_layout",
]
