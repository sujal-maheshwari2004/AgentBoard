"""ClaudeBridge against a fake Claude Code inbox socket (CONTRACTS §9)."""

from __future__ import annotations

import asyncio
import json
import shutil
import socket
import tempfile
import threading
from pathlib import Path

import pytest

from whiteboard.server.claude_bridge import ClaudeBridge, format_push


class FakeInbox:
    """A Unix socket server that records every newline-delimited JSON line."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.connections: list[list[dict]] = []
        self._srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._srv.bind(str(path))
        self._srv.listen(4)
        self._srv.settimeout(0.2)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._srv.accept()
            except socket.timeout:
                continue
            with conn:
                conn.settimeout(2)
                data = b""
                try:
                    while True:
                        chunk = conn.recv(65536)
                        if not chunk:
                            break
                        data += chunk
                except socket.timeout:
                    pass
            lines = [json.loads(line) for line in data.decode().splitlines() if line.strip()]
            self.connections.append(lines)

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2)
        self._srv.close()


@pytest.fixture
def sock_dir():
    """AF_UNIX paths are capped at ~104 bytes on macOS; pytest's tmp_path is too deep."""
    d = Path(tempfile.mkdtemp(prefix="wb"))
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def inbox(sock_dir: Path):
    box = FakeInbox(sock_dir / "cc.sock")
    yield box
    box.close()


def test_format_push_matches_contract() -> None:
    text = format_push("AgentBoard", 57, ['chat (to: root): "hi"', "edit: node-a renamed"], 56)
    assert text == (
        "[whiteboard seq=57 project=AgentBoard]\n"
        '- chat (to: root): "hi"\n'
        "- edit: node-a renamed\n"
        "Use mcp__whiteboard__get_events(since_seq=56) for full payloads."
    )


async def test_push_sends_auth_then_user_line(inbox: FakeInbox) -> None:
    seq = {"n": 12}
    bridge = ClaudeBridge("proj", str(inbox.path), "tok-1", seq_provider=lambda: seq["n"], debounce_s=0.05)
    assert bridge.ok and bridge.status()["socket"] == str(inbox.path)
    bridge.queue_line('chat (to: root): "hello"')
    bridge.queue_line("edit: node-a renamed to \"A2\"")
    ok = await bridge.push_now()
    assert ok is True
    for _ in range(50):
        if inbox.connections:
            break
        await asyncio.sleep(0.05)
    assert len(inbox.connections) == 1
    auth, user = inbox.connections[0]
    assert auth == {"type": "auth", "token": "tok-1"}
    assert user["type"] == "user"
    assert user["text"] == format_push("proj", 12, ['chat (to: root): "hello"', 'edit: node-a renamed to "A2"'], 0)
    st = bridge.status()
    assert st["last_pushed_seq"] == 12 and st["failures"] == 0 and st["last_error"] is None and st["queued"] == 0
    # Next push references the previous pushed seq and only carries new lines.
    seq["n"] = 15
    bridge.queue_line("status: node-b → done")
    await bridge.push_now()
    for _ in range(50):
        if len(inbox.connections) == 2:
            break
        await asyncio.sleep(0.05)
    assert inbox.connections[1][1]["text"].startswith("[whiteboard seq=15 project=proj]\n- status: node-b → done\nUse mcp__whiteboard__get_events(since_seq=12)")
    assert bridge.last_pushed_seq == 15


async def test_debounce_folds_lines_into_one_message(inbox: FakeInbox) -> None:
    bridge = ClaudeBridge("proj", str(inbox.path), None, seq_provider=lambda: 3, debounce_s=0.05)
    for i in range(5):
        bridge.queue_line(f"line {i}")
    await asyncio.sleep(0.4)
    assert len(inbox.connections) == 1
    lines = inbox.connections[0]
    assert [l["type"] for l in lines] == ["user"]  # no token -> no auth line
    assert lines[0]["text"].count("\n- ") == 5


async def test_missing_socket_keeps_lines_and_reports_failure(sock_dir: Path) -> None:
    bridge = ClaudeBridge("proj", str(sock_dir / "missing.sock"), "t", seq_provider=lambda: 1, debounce_s=0.01)
    bridge.queue_line("a")
    assert await bridge.push_now() is False
    st = bridge.status()
    assert st["ok"] is False and st["failures"] == 1 and "FileNotFoundError" in st["last_error"]
    assert st["queued"] == 1 and st["last_pushed_seq"] == 0
    # Retarget to a working socket: the queued line goes out on the next flush.
    box = FakeInbox(sock_dir / "later.sock")
    try:
        bridge.retarget(str(box.path), "t2")
        assert bridge.failures == 0 and bridge.ok
        assert await bridge.push_now() is True
        for _ in range(50):
            if box.connections:
                break
            await asyncio.sleep(0.05)
        assert box.connections[0][0]["token"] == "t2" and "- a\n" in box.connections[0][1]["text"]
        assert bridge.status()["queued"] == 0
    finally:
        box.close()


async def test_no_socket_configured(monkeypatch) -> None:
    bridge = ClaudeBridge("proj")
    assert bridge.socket_path is None and bridge.ok is False
    bridge.queue_line("x")
    assert await bridge.push_now() is False
    assert bridge.status()["last_error"] == "no session socket configured"
    assert bridge.failures == 0  # not counted as a delivery failure


def test_queue_cap_drops_oldest() -> None:
    bridge = ClaudeBridge("proj", "/nonexistent", max_lines=3)
    for i in range(5):
        bridge.queue_line(f"l{i}")
    assert list(bridge._lines) == ["l2", "l3", "l4"] and bridge.dropped == 2
    bridge.queue_line("   ")  # blank lines are ignored
    assert len(bridge._lines) == 3
