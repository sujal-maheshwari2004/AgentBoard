"""Shared fixtures. The autouse fixture keeps every test away from the developer's
real Claude Code session socket and `~/.whiteboard/registry.json`."""

from __future__ import annotations

import queue
import threading
from pathlib import Path

import pytest

from whiteboard.files.atomic import SelfWriteRegistry
from whiteboard.plan.store import PlanStore
from whiteboard.scaffold import scaffold


@pytest.fixture(autouse=True)
def _isolate_env(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("CLAUDE_CODE_MESSAGING_SOCKET", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_MESSAGING_TOKEN", raising=False)
    monkeypatch.delenv("WHITEBOARD_PORT", raising=False)
    monkeypatch.setenv("WHITEBOARD_REGISTRY", str(tmp_path / "_registry.json"))


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A scaffolded project with three nodes: b depends on a; c is independent."""
    root = tmp_path / "proj"
    root.mkdir()
    scaffold(root)
    store = PlanStore(root, self_writes=SelfWriteRegistry())
    store.load()
    store.upsert_node("node-a", title="A thing")
    store.upsert_node("node-b", title="B thing", depends_on=["node-a"])
    store.upsert_node("node-c", title="C thing")
    return root


def hello(ws, last_seq: int = 0, client_id: str = "test") -> tuple[dict, list[dict]]:
    """Send client.hello; return (snapshot, everything up to and including bridge.status)."""
    ws.send_json({"type": "client.hello", "payload": {"clientId": client_id, "lastSeq": last_seq, "protocol": 1}, "seq": 1})
    snap = ws.receive_json()
    assert snap["type"] == "plan.snapshot", snap
    rest: list[dict] = []
    while True:
        m = ws.receive_json()
        rest.append(m)
        if m["type"] == "bridge.status":
            return snap, rest


def recv_until(ws, wanted: str, *, timeout: float = 5.0, limit: int = 50) -> tuple[dict, list[dict]]:
    """Read frames until one of type ``wanted`` arrives (or ``timeout``); returns (it, all_read)."""
    seen: list[dict] = []
    box: queue.Queue = queue.Queue()

    def reader() -> None:
        try:
            box.put(("ok", ws.receive_json()))
        except BaseException as exc:  # noqa: BLE001 - surfaced to the test
            box.put(("err", exc))

    for _ in range(limit):
        threading.Thread(target=reader, daemon=True).start()
        try:
            kind, value = box.get(timeout=timeout)
        except queue.Empty:
            raise AssertionError(f"timed out waiting for {wanted!r}; saw {[m['type'] for m in seen]}") from None
        if kind == "err":
            raise AssertionError(f"socket error while waiting for {wanted!r}: {value!r}; saw {[m['type'] for m in seen]}")
        seen.append(value)
        if value["type"] == wanted:
            return value, seen
    raise AssertionError(f"never saw {wanted!r} in {[m['type'] for m in seen]}")
