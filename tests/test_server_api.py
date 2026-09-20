"""HTTP routes, static/SPA serving, the /mcp mount and run_foreground."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import threading
import time
from pathlib import Path

import httpx2
import pytest
from mcp.client import Client
from mcp.server import MCPServer
from starlette.testclient import TestClient

from whiteboard import config
from whiteboard.liaison import registry_read
from whiteboard.server import app as app_mod
from whiteboard.server.app import MISSING_CANVAS_HTML, create_app, run_foreground


@pytest.fixture
def client(project: Path):
    app = create_app(project)
    with TestClient(app) as c:
        c.app_ref = app  # type: ignore[attr-defined]
        yield c


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ------------------------------------------------------------------- health / registry
def test_health_and_registry(client: TestClient, project: Path) -> None:
    body = client.get("/api/health").json()
    assert body["project"] == str(project.resolve()) and body["pid"] == os.getpid()
    assert body["nodes"] == 3 and body["agents"] == [] and body["clients"] == 0 and body["latest_seq"] == 0
    assert body["agent_count"] == 0 and body["pending_diagrams"] == 0
    assert isinstance(body["rev"], int) and body["uptime_s"] >= 0 and body["port"] == 0
    assert set(body["bridge"]) >= {"ok", "failures", "last_error", "last_pushed_seq", "socket"}
    reg = registry_read()
    assert reg[str(project.resolve())]["pid"] == os.getpid()


def test_registry_entry_removed_on_shutdown(project: Path) -> None:
    with TestClient(create_app(project)):
        assert str(project.resolve()) in registry_read()
    assert str(project.resolve()) not in registry_read()
    log_text = (project / ".whiteboard" / "server.log").read_text()
    lines = [json.loads(line) for line in log_text.splitlines() if line.strip()]
    assert any("server up" in entry["msg"] for entry in lines) and all(entry["ts"].endswith("Z") for entry in lines)


# ------------------------------------------------------------------- events / session
def test_events_backfill(client: TestClient) -> None:
    log = client.app_ref.state.log  # type: ignore[attr-defined]
    for i in range(5):
        log.append(agent_id="root", node_id=None, type="info", note=f"n{i}")
    body = client.get("/api/events?since=2&limit=2").json()
    assert [e["seq"] for e in body["events"]] == [3, 4] and body["latest_seq"] == 5
    assert body["bridge"]["last_pushed_seq"] == 0 and body["since"] == 2
    assert [e["seq"] for e in client.get("/api/events").json()["events"]] == [1, 2, 3, 4, 5]


def test_threads_endpoints(client: TestClient) -> None:
    log = client.app_ref.state.log  # type: ignore[attr-defined]
    log.append(agent_id="user", node_id=None, type="chat", note="hi root",
               data={"from": "user", "to": "root", "thread": "root"})
    log.append(agent_id="root", node_id=None, type="chat", note="hi back",
               data={"from": "root", "to": "user", "thread": "root", "reply_to": "chat-1"})
    log.append(agent_id="user", node_id="node-a", type="chat", note="status?",
               data={"from": "user", "to": "agent-parser", "thread": "agent-parser"})
    log.append(agent_id="root", node_id=None, type="info", note="not chat")

    body = client.get("/api/threads").json()
    assert set(body["threads"]) == {"root", "agent-parser"}
    assert body["threads"]["root"]["count"] == 2 and body["threads"]["root"]["last_seq"] == 2
    assert body["threads"]["root"]["last_ts"].endswith("Z")
    assert body["threads"]["agent-parser"] == {
        "count": 1, "last_seq": 3, "last_ts": body["threads"]["agent-parser"]["last_ts"],
    }

    thread = client.get("/api/threads/root").json()
    assert thread["thread"] == "root" and thread["latest_seq"] == 2
    assert [m["id"] for m in thread["messages"]] == ["chat-1", "chat-2"]
    assert thread["messages"][1]["reply_to"] == "chat-1" and thread["messages"][1]["from"] == "root"

    after = client.get("/api/threads/root?since=1").json()
    assert [m["seq"] for m in after["messages"]] == [2] and after["latest_seq"] == 2
    capped = client.get("/api/threads/root?since=0&limit=1").json()
    assert [m["seq"] for m in capped["messages"]] == [1] and capped["latest_seq"] == 1
    empty = client.get("/api/threads/agent-zz?since=7").json()
    assert empty == {"thread": "agent-zz", "messages": [], "latest_seq": 7}


def test_health_agents_list(client: TestClient) -> None:
    store = client.app_ref.state.store  # type: ignore[attr-defined]
    store.upsert_agent("agent-a", "node-a")
    store.touch_agent("agent-a", activity="parsing", progress=2.0, metrics={"tool_calls": 7})
    body = client.get("/api/health").json()
    assert body["agent_count"] == 1 and len(body["agents"]) == 1
    row = body["agents"][0]
    assert set(row) == {"id", "assigned_node", "status", "activity", "progress",
                        "spawned_at", "heartbeat_at", "finished_at", "metrics"}
    assert row["id"] == "agent-a" and row["activity"] == "parsing"
    assert row["progress"] == 1.0 and row["metrics"] == {"tool_calls": 7}


def test_session_retarget(client: TestClient, tmp_path: Path) -> None:
    sock = tmp_path / "cc.sock"
    r = client.post("/api/session", json={"socket": str(sock), "token": "tok"})
    assert r.status_code == 200
    assert r.json()["bridge"]["socket"] == str(sock) and r.json()["bridge"]["ok"] is True
    bridge = client.app_ref.state.bridge  # type: ignore[attr-defined]
    assert bridge.token == "tok"
    assert client.get("/api/health").json()["bridge"]["socket"] == str(sock)
    r = client.post("/api/session", json={"socket": None})
    assert r.json()["bridge"]["socket"] is None


# ------------------------------------------------------------------- skeleton / nodes / snapshot / debug / paste
def test_skeleton_nodes_snapshot_debug(client: TestClient) -> None:
    skel = client.get("/skeleton").json()
    assert skel["project"] == "proj"
    assert {n["id"] for n in skel["nodes"]} == {"node-a", "node-b", "node-c"}
    assert next(n for n in skel["nodes"] if n["id"] == "node-b")["depends_on"] == ["node-a"]
    node = client.get("/api/nodes/node-b").json()
    assert node["title"] == "B thing" and node["path"] == ".whiteboard/plan/nodes/node-b.md"
    assert client.get("/api/nodes/node-zzz").status_code == 404
    snap = client.get("/api/snapshot").json()
    assert len(snap["nodes"]) == 3 and [d["name"] for d in snap["diagrams"]][0] == "hld"
    dbg = client.get("/api/debug/state").json()
    assert set(dbg) >= {"snapshot", "last_good", "invalid", "pending_edits", "pending_prompts"}
    assert "node-a" in dbg["last_good"]["nodes"] and dbg["pending_edits"] == {}


def test_api_paste(client: TestClient) -> None:
    r = client.post("/api/paste", json={"text": "flowchart TD\n  node-m[M] --> node-n[N]\n"})
    assert r.status_code == 200 and r.json()["ingested"] == "diagram"
    assert "node-n" in client.app_ref.state.store.nodes  # type: ignore[attr-defined]
    r = client.post("/api/paste", json={"text": "just some thoughts"})
    assert r.json()["ingested"] == "text"
    events = client.get("/api/events").json()["events"]
    assert [e["type"] for e in events] == ["node_changed", "plan_pasted"]
    assert client.post("/api/paste", json={"text": "  "}).status_code == 400


# ------------------------------------------------------------------- canvas / SPA
def test_index_assets_and_spa_fallback(client: TestClient) -> None:
    dist = app_mod.canvas_dist()
    r = client.get("/")
    assert r.status_code == 200 and "text/html" in r.headers["content-type"]
    if (dist / "index.html").is_file():
        assert "<div id=\"root\">" in r.text
        asset = next((dist / "assets").glob("*.js"))
        assert client.get(f"/assets/{asset.name}").status_code == 200
    else:  # pragma: no cover - only when canvas/dist was not built
        assert "canvas build missing" in r.text
    # unknown client-side route -> index (SPA)
    assert client.get("/some/client/route").text == r.text
    assert client.get("/api/nope").status_code == 404
    assert client.get("/assets/missing.js").status_code == 404


def test_missing_canvas_build_page(project: Path, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("WHITEBOARD_CANVAS_DIST", str(tmp_path / "nodist"))
    with TestClient(create_app(project)) as c:
        r = c.get("/")
        assert r.status_code == 200 and "canvas build missing" in r.text and r.text == MISSING_CANVAS_HTML
        assert "pnpm build" in c.get("/anything").text


def test_path_traversal_guard(client: TestClient, project: Path) -> None:
    for path in ("/%2e%2e/pyproject.toml", "/..%2fpyproject.toml", "/assets/../../pyproject.toml", "/%2e%2e%2f%2e%2e%2fetc%2fpasswd"):
        r = client.get(path)
        assert r.status_code == 404 or "[project]" not in r.text, path
    assert app_mod._safe_child(Path("/x/dist"), "../secret") is None
    assert app_mod._safe_child(Path("/x/dist"), "a//b") is None
    assert app_mod._safe_child(Path("/x/dist"), "a/b.js") == Path("/x/dist/a/b.js").resolve()


# ------------------------------------------------------------------- /mcp mount
def _tiny_mcp_factory(store, log, bus, root) -> MCPServer:
    srv = MCPServer("t")

    @srv.tool()
    def ping() -> str:
        return f"pong from {root.name}"

    return srv


def test_mcp_mount_is_at_slash_mcp(project: Path) -> None:
    app = create_app(project, mcp_factory=_tiny_mcp_factory)
    with TestClient(app, base_url="http://127.0.0.1:43000") as c:
        headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
        init = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}}
        r = c.post("/mcp", json=init, headers=headers)
        assert r.status_code == 200, r.text
        assert r.json()["result"]["serverInfo"]["name"] == "t"
        assert c.post("/mcp/mcp", json=init, headers=headers).status_code == 404
        assert c.get("/mcp").status_code == 405
        # regular routes still work next to the mount
        assert c.get("/api/health").status_code == 200


@pytest.mark.slow
def test_mcp_client_end_to_end_over_uvicorn(project: Path) -> None:
    import uvicorn

    port = _free_port()
    app = create_app(project, mcp_factory=_tiny_mcp_factory)
    app.state.port = port
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_config=None, lifespan="on"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        _wait_health(port)

        async def go() -> tuple[list[str], str]:
            async with Client(f"http://127.0.0.1:{port}/mcp") as c:
                tools = await c.list_tools()
                res = await c.call_tool("ping", {})
                return [t.name for t in tools.tools], res.content[0].text

        names, text = asyncio.run(go())
        assert names == ["ping"] and text == "pong from proj"
    finally:
        server.should_exit = True
        thread.join(timeout=10)
    assert not thread.is_alive()


def test_real_mcp_server_is_mounted_by_default(project: Path) -> None:
    app = create_app(project)
    assert app.state.mcp is not None and app.state.mcp.name == "whiteboard"
    with TestClient(app, base_url="http://127.0.0.1:43000") as c:
        headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
        init = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}}
        r = c.post("/mcp", json=init, headers=headers)
        assert r.status_code == 200 and r.json()["result"]["serverInfo"]["name"] == "whiteboard"


# ------------------------------------------------------------------- run_foreground
def _wait_health(port: int, timeout: float = 8.0) -> dict:
    deadline = time.monotonic() + timeout
    last: Exception | None = None
    while time.monotonic() < deadline:
        try:
            r = httpx2.get(f"http://127.0.0.1:{port}/api/health", timeout=0.5)
            if r.status_code == 200:
                return r.json()
        except Exception as exc:  # noqa: BLE001
            last = exc
        time.sleep(0.1)
    raise AssertionError(f"server on {port} never became healthy: {last!r}")


@pytest.mark.slow
def test_run_foreground_honors_env_port_and_writes_server_json(project: Path, monkeypatch) -> None:
    port = _free_port()
    monkeypatch.setenv("WHITEBOARD_PORT", str(port))
    config.write_server_json(project, port)  # what the CLI's _serve does before calling us
    holder: list = []
    thread = threading.Thread(target=run_foreground, args=(project,), kwargs={"on_server": holder.append}, daemon=True)
    thread.start()
    try:
        body = _wait_health(port)
        assert body["port"] == port and body["pid"] == os.getpid() and body["project"] == str(project.resolve())
        info = config.read_server_json(project)
        assert info["port"] == port and info["pid"] == os.getpid() and info["mcp_url"].endswith(f":{port}/mcp")
        assert registry_read()[str(project.resolve())]["port"] == port
    finally:
        assert holder, "on_server was not called"
        holder[0].should_exit = True
        thread.join(timeout=10)
    assert not thread.is_alive()
    assert str(project.resolve()) not in registry_read()


@pytest.mark.slow
def test_run_foreground_falls_back_when_env_port_busy(project: Path, monkeypatch) -> None:
    blocker = socket.socket()
    blocker.bind(("127.0.0.1", 0))
    blocker.listen(1)
    busy = blocker.getsockname()[1]
    monkeypatch.setenv("WHITEBOARD_PORT", str(busy))
    config.write_server_json(project, busy)
    holder: list = []
    thread = threading.Thread(target=run_foreground, args=(project,), kwargs={"on_server": holder.append}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 8
        info = None
        while time.monotonic() < deadline:
            info = config.read_server_json(project)
            if info and info["port"] != busy:
                break
            time.sleep(0.1)
        assert info and info["port"] != busy, info
        body = _wait_health(info["port"])
        assert body["port"] == info["port"] and os.environ["WHITEBOARD_PORT"] == str(info["port"])
    finally:
        blocker.close()
        if holder:
            holder[0].should_exit = True
        thread.join(timeout=10)
