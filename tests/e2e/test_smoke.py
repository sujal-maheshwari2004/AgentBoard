"""End-to-end smoke test against a REAL daemon (PRD verification 1-7, minus the browser).

The whole loop, driven the way the skill and the canvas drive it:

* ``whiteboard scaffold`` + ``whiteboard start --no-open`` spawn the detached server via
  ``uv run --project <this repo> whiteboard _serve`` (``AGENTBOARD_HOME`` points at this checkout);
* a real websocket client (``websockets``) plays the canvas;
* a real MCP client (``mcp.client.Client``) over streamable HTTP plays the root session / subagents;
* a fake Claude Code inbox socket records what the ``ClaudeBridge`` pushes;
* an editor edit is a real atomic write on disk picked up by the watchdog observer;
* ``whiteboard stop`` / ``start`` proves the state survives a restart (files only, nothing in memory).

Everything that holds a Unix socket lives under ``/tmp`` (macOS caps AF_UNIX paths at ~104 bytes).
Every wait polls with a deadline; the only fixed sleeps are the 0.1 s poll intervals.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx2
import pytest
from mcp.client import Client
from websockets.sync.client import connect as ws_connect

from tests.test_server_bridge import FakeInbox
from whiteboard.files.frontmatter import split_frontmatter

pytestmark = pytest.mark.slow

REPO = Path(__file__).resolve().parent.parent.parent
CLI = [sys.executable, "-m", "whiteboard.cli"]
POLL_S = 0.1
TOKEN = "e2e-token-0123"


# --------------------------------------------------------------------------- helpers
def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for(pred: Callable[[], Any], *, timeout: float, what: str) -> Any:
    """Poll ``pred`` every 100 ms until it returns a truthy value; assert on timeout."""
    deadline = time.monotonic() + timeout
    while True:
        value = pred()
        if value:
            return value
        if time.monotonic() >= deadline:
            raise AssertionError(f"timed out after {timeout:.1f}s waiting for {what}")
        time.sleep(POLL_S)


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_name(f".{path.name}.e2e.tmp")  # leading dot: the watcher ignores the temp file
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _read_events(project: Path) -> list[dict]:
    text = (project / ".whiteboard" / "events.jsonl").read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def _node_meta(project: Path, node_id: str) -> tuple[dict, str]:
    return split_frontmatter((project / ".whiteboard" / "plan" / "nodes" / f"{node_id}.md").read_text(encoding="utf-8"))


def _shape(snapshot: dict) -> dict:
    """The restart-invariant projection of a snapshot: nodes, edges, agents, statuses."""
    return {
        "nodes": {
            n["id"]: (n["title"], n["type"], n["status"], n["owner"], sorted(n["depends_on"]), n["body"])
            for n in snapshot["nodes"]
        },
        "edges": sorted((e["src"], e["dst"], e["diagram"]) for e in snapshot["edges"]),
        "agents": {a["id"]: (a["assigned_node"], a["status"], sorted(a["ready_deps"])) for a in snapshot["agents"]},
    }


class Cli:
    """Runs the ``whiteboard`` CLI as a subprocess with the daemon's environment."""

    def __init__(self, project: Path, env: dict[str, str]) -> None:
        self.project = project
        self.env = env

    def run(self, *args: str, check: bool = True, timeout: float = 60.0) -> dict:
        proc = subprocess.run(
            [*CLI, *args, "--project-dir", str(self.project)],
            env=self.env, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL, cwd=str(REPO),
        )
        try:
            out = json.loads(proc.stdout) if proc.stdout.strip() else {}
        except ValueError:
            out = {"raw": proc.stdout}
        if check:
            assert proc.returncode == 0, f"whiteboard {' '.join(args)} failed ({proc.returncode}):\n{proc.stdout}\n{proc.stderr}"
        return out

    def start(self) -> dict:
        info = self.run("start", "--no-open", "--timeout", "45")
        assert info.get("url") and info.get("pid"), info
        _wait_for(lambda: _health(info["port"]), timeout=10, what="/api/health after start")
        return info

    def stop(self) -> dict:
        return self.run("stop")


def _health(port: int) -> dict | None:
    try:
        r = httpx2.get(f"http://127.0.0.1:{port}/api/health", timeout=1.0)
    except Exception:
        return None
    return r.json() if r.status_code == 200 else None


def _get(port: int, path: str) -> Any:
    r = httpx2.get(f"http://127.0.0.1:{port}{path}", timeout=5.0)
    assert r.status_code == 200, (path, r.status_code, r.text)
    return r.json()


def mcp_call(port: int, tool: str, **args: Any) -> dict:
    """One stateless MCP call over streamable HTTP; returns the tool's JSON dict."""

    async def go():
        async with Client(f"http://127.0.0.1:{port}/mcp") as c:
            return await c.call_tool(tool, args)

    result = asyncio.run(go())
    text = result.content[0].text if result.content else ""
    assert not result.is_error, f"{tool}({args}) -> {text}"
    return json.loads(text)


class Canvas:
    """A websocket client speaking the CONTRACTS §6 envelope, like the tldraw canvas."""

    def __init__(self, port: int) -> None:
        self.sock = ws_connect(f"ws://127.0.0.1:{port}/ws", open_timeout=5, legacy=True)
        self.seq = 0
        self.seen: list[dict] = []

    def close(self) -> None:
        self.sock.close()

    def send(self, type: str, payload: dict) -> int:
        self.seq += 1
        self.sock.send(json.dumps({"type": type, "payload": payload, "seq": self.seq}))
        return self.seq

    def mark(self) -> int:
        """Position in the frame history; pass it to ``wait(since=...)`` to include frames already read."""
        return len(self.seen)

    def wait(self, type: str, pred: Callable[[dict], bool] | None = None, *, timeout: float = 5.0, since: int | None = None) -> dict:
        """Read frames until one of ``type`` (satisfying ``pred``) arrives. With ``since`` (a
        :meth:`mark`), frames already consumed by an earlier ``wait`` are considered first, so
        two waits after one send do not depend on the server's broadcast order."""
        if since is not None:
            for msg in self.seen[since:]:
                if msg["type"] == type and (pred is None or pred(msg)):
                    return msg
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssertionError(f"timed out waiting for {type!r}; last frames: {[m['type'] for m in self.seen[-25:]]}")
            try:
                raw = self.sock.recv(timeout=remaining)
            except TimeoutError:
                continue
            msg = json.loads(raw)
            self.seen.append(msg)
            if msg["type"] == type and (pred is None or pred(msg)):
                return msg

    def hello(self, client_id: str = "e2e", last_seq: int = 0) -> tuple[dict, dict]:
        self.send("client.hello", {"clientId": client_id, "lastSeq": last_seq, "protocol": 1})
        snapshot = self.wait("plan.snapshot")
        bridge = self.wait("bridge.status")
        return snapshot["payload"], bridge["payload"]


class Stopwatch:
    def __init__(self) -> None:
        self.laps: list[tuple[str, float]] = []
        self._t = time.monotonic()

    def lap(self, name: str) -> None:
        now = time.monotonic()
        self.laps.append((name, round(now - self._t, 3)))
        self._t = now

    def report(self) -> str:
        total = sum(t for _, t in self.laps)
        rows = "\n".join(f"  {name:<44} {t:7.3f}s" for name, t in self.laps)
        return f"e2e timings:\n{rows}\n  {'total':<44} {total:7.3f}s"


# --------------------------------------------------------------------------- the test
def test_full_loop_against_real_daemon() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="wbe2e", dir="/tmp"))
    project = tmp / "proj"
    project.mkdir()
    registry = tmp / "registry.json"
    inbox = FakeInbox(tmp / "cc.sock")
    env = {
        **os.environ,
        "AGENTBOARD_HOME": str(REPO),
        "WHITEBOARD_REGISTRY": str(registry),
        "WHITEBOARD_PORT": str(_free_port()),  # hint only: `_serve` picks the project's hashed port itself
        "CLAUDE_CODE_MESSAGING_SOCKET": str(inbox.path),
        "CLAUDE_CODE_MESSAGING_TOKEN": TOKEN,
        "PYTHONUNBUFFERED": "1",
    }
    env.pop("VIRTUAL_ENV", None)
    cli = Cli(project, env)
    clock = Stopwatch()
    canvas: Canvas | None = None
    info: dict = {}
    try:
        # -- scaffold + start -------------------------------------------------------------
        scaffolded = cli.run("scaffold")
        assert ".whiteboard/COLLABORATION.md" in scaffolded["created"]
        assert (project / ".claude" / "agents" / "whiteboard-task.md").is_file()
        info = cli.start()
        port = int(info["port"])
        health = _health(port)
        assert health and health["project"] == str(project.resolve()) and health["nodes"] == 0
        assert health["bridge"]["socket"] == str(inbox.path), health["bridge"]
        reg = json.loads(registry.read_text())
        assert reg[str(project.resolve())]["port"] == port, reg
        clock.lap("scaffold + start + health")

        # -- a. websocket hello -> snapshot ---------------------------------------------
        canvas = Canvas(port)
        snapshot, bridge = canvas.hello()
        assert snapshot["nodes"] == [] and snapshot["agents"] == [] and snapshot["project"] == "proj"
        assert {d["name"] for d in snapshot["diagrams"]} >= {"hld", "er"}
        assert bridge["ok"] is True and bridge["socket"] == str(inbox.path)
        clock.lap("a. ws hello")

        # -- b. MCP: three nodes with deps + diagram ---------------------------------------
        a = mcp_call(port, "upsert_node", id="node-a", title="A thing", body="Body of A.")
        assert a["id"] == "node-a" and a["status"] == "todo"
        mcp_call(port, "upsert_node", id="node-b", title="B thing", depends_on=["node-a"], body="Body of B.")
        mcp_call(port, "upsert_node", id="node-c", title="C thing", type="lld", depends_on=["node-b"], interfaces=["c() -> int"])
        diagram = mcp_call(
            port, "write_diagram", name="hld",
            mermaid="flowchart TD\n    node-a[A thing]\n    node-b[B thing]\n    node-c[C thing]\n    node-a --> node-b\n    node-b --> node-c\n",
        )
        assert sorted((e["src"], e["dst"]) for e in diagram["edges"]) == [("node-a", "node-b"), ("node-b", "node-c")]
        nodes_dir = project / ".whiteboard" / "plan" / "nodes"
        assert {p.name for p in nodes_dir.glob("node-*.md")} == {"node-a.md", "node-b.md", "node-c.md"}
        meta_c, _ = _node_meta(project, "node-c")
        assert meta_c["depends_on"] == ["node-b"] and meta_c["type"] == "lld" and meta_c["interfaces"] == ["c() -> int"]
        hld = (project / ".whiteboard" / "plan" / "hld.md").read_text()
        assert "node-a --> node-b" in hld and "node-b --> node-c" in hld
        plan_md = (project / ".whiteboard" / "PLAN.md").read_text()
        assert all(n in plan_md for n in ("node-a", "node-b", "node-c")) and "A thing" in plan_md
        for nid in ("node-a", "node-b", "node-c"):
            canvas.wait("plan.node.upsert", lambda m, nid=nid: m["payload"]["node"]["id"] == nid, timeout=3)
        assert mcp_call(port, "status")["nodes"] == 3
        clock.lap("b. MCP upsert_node x3 + write_diagram")

        # -- c. editor edit on disk -> watcher -> ws ----------------------------------------
        path_b = nodes_dir / "node-b.md"
        _atomic_write(path_b, path_b.read_text(encoding="utf-8").rstrip("\n") + "\n\nAdded by the e2e editor.\n")
        up = canvas.wait(
            "plan.node.upsert",
            lambda m: m["payload"]["node"]["id"] == "node-b" and "Added by the e2e editor." in m["payload"]["node"]["body"],
            timeout=3,
        )
        assert up["payload"]["node"]["title"] == "B thing"  # frontmatter untouched
        clock.lap("c. on-disk edit -> plan.node.upsert")

        # -- d. cosmetic canvas edit: rename -----------------------------------------------
        seq = canvas.send("canvas.edit", {"ops": [{"op": "renamed", "id": "node-b", "label": "B renamed"}]})
        ack = canvas.wait("edit.ack", lambda m: m["payload"]["forSeq"] == seq)
        assert ack["replyTo"] == seq
        meta_b, body_b = _node_meta(project, "node-b")
        assert meta_b["title"] == "B renamed" and "Added by the e2e editor." in body_b
        clock.lap("d. canvas.edit rename -> edit.ack")

        # -- e. risky canvas edit: new edge, approved with a note ---------------------------
        seq = canvas.send("canvas.edit", {"ops": [{"op": "edge-created", "from": "node-a", "to": "node-c", "diagram": "hld"}]})
        req = canvas.wait("risky_edit.request", lambda m: m["replyTo"] == seq)
        rid = req["payload"]["request_id"]
        assert "node-c" in req["payload"]["affected"]
        meta_c, _ = _node_meta(project, "node-c")
        assert meta_c["depends_on"] == ["node-b"], "parked edit must not touch the file before approval"
        mark = canvas.mark()
        canvas.send("risky_edit.reply", {"request_id": rid, "approved": True, "note": "c really needs a"})
        canvas.wait("edit.ack", lambda m: m["payload"]["forSeq"] == seq, since=mark)
        accepted = canvas.wait("event.append", lambda m: m["payload"]["type"] == "risky_edit_accepted", since=mark)
        assert accepted["payload"]["data"]["request_id"] == rid and "c really needs a" in accepted["payload"]["note"]
        meta_c, _ = _node_meta(project, "node-c")
        assert set(meta_c["depends_on"]) == {"node-a", "node-b"}
        assert "node-a --> node-c" in (project / ".whiteboard" / "plan" / "hld.md").read_text()
        logged = [e for e in _read_events(project) if e["type"] == "risky_edit_accepted"]
        assert logged and logged[-1]["data"]["request_id"] == rid and logged[-1]["node_id"] == "node-c"
        clock.lap("e. risky edge edit -> approve -> file + event")

        # -- f. dispatch: propose via MCP, approve on the canvas ----------------------------
        proposed = mcp_call(port, "propose_dispatch", node_id="node-a", agent_id="agent-a", job_spec_md="")
        req = canvas.wait("dispatch.request", lambda m: m["payload"]["request_id"] == proposed["request_id"])
        assert req["payload"]["node_id"] == "node-a" and req["payload"]["agent_id"] == "agent-a"
        assert "node-a" in req["payload"]["job_spec_md"]
        assert (project / ".whiteboard" / "agents" / "agent-a" / "plan.md").read_text() == req["payload"]["job_spec_md"]
        mark = canvas.mark()
        canvas.send("dispatch.reply", {"request_id": proposed["request_id"], "approved": True, "note": "go"})
        approved = canvas.wait("event.append", lambda m: m["payload"]["type"] == "dispatch_approved", since=mark)
        assert approved["payload"]["data"]["agent_id"] == "agent-a" and approved["payload"]["notified"] == ["agent-a"]
        canvas.wait("plan.node.upsert", lambda m: m["payload"]["node"]["id"] == "node-a" and m["payload"]["node"]["owner"] == "agent-a", since=mark)
        assert _node_meta(project, "node-a")[0]["owner"] == "agent-a"
        assert _get(port, "/api/nodes/node-a")["owner"] == "agent-a"
        assert any(e["type"] == "dispatch_approved" for e in _read_events(project))
        clock.lap("f. propose_dispatch -> dispatch.reply -> owner")

        # -- g. agent-a finishes: dependents' cards go ready, notified is recorded ---------
        mcp_call(port, "upsert_agent", agent_id="agent-b", assigned_node="node-b", plan_md="# agent-b\nwait for a")
        canvas.wait("agent.card.upsert", lambda m: m["payload"]["agent"]["id"] == "agent-b")
        mark = canvas.mark()
        done = mcp_call(port, "append_event", agent_id="agent-a", node_id="node-a", type="done", note="A landed")
        assert done["type"] == "done" and "agent-b" in done["notified"] and done["data"]["dispatchable"] == ["node-b"]
        card = canvas.wait(
            "agent.card.upsert",
            lambda m: m["payload"]["agent"]["id"] == "agent-b" and "node-a" in m["payload"]["agent"]["ready_deps"],
            since=mark,
        )
        canvas.wait("agent.card.upsert", lambda m: m["payload"]["agent"]["id"] == "agent-a" and m["payload"]["agent"]["status"] == "done", since=mark)
        canvas.wait("event.append", lambda m: m["payload"]["type"] == "done" and m["payload"]["seq"] == done["seq"], since=mark)
        assert card["payload"]["agent"]["assigned_node"] == "node-b"
        done_events = [e for e in _read_events(project) if e["type"] == "done"]
        assert len(done_events) == 1 and "agent-b" in done_events[0]["notified"] and done_events[0]["agent_id"] == "agent-a"
        card_meta, _ = split_frontmatter((project / ".whiteboard" / "agents" / "agent-b" / "card.md").read_text())
        assert card_meta["ready_deps"] == ["node-a"]
        assert _node_meta(project, "node-a")[0]["status"] == "done"
        assert [n["id"] for n in mcp_call(port, "get_dispatchable")["nodes"]] == ["node-b"]
        clock.lap("g. append_event done -> ready_deps + notified")

        # -- h. chat to an agent reaches the (fake) Claude Code inbox socket -----------------
        canvas.send("chat.message", {"text": "hello agent-a from the canvas", "agentId": "agent-a"})

        def chat_pushed() -> dict | None:
            for lines in list(inbox.connections):
                for line in lines:
                    if line.get("type") == "user" and "chat (to: agent-a, thread agent-a)" in line.get("text", ""):
                        return line
            return None

        pushed = _wait_for(chat_pushed, timeout=5, what="the chat line on the inbox socket")
        assert pushed["text"].startswith("[whiteboard seq=") and "project=proj]" in pushed["text"]
        assert '"hello agent-a from the canvas"' in pushed["text"]
        assert "mcp__whiteboard__get_events(since_seq=" in pushed["text"]
        for lines in inbox.connections:
            assert lines[0] == {"type": "auth", "token": TOKEN}, lines
            assert [line["type"] for line in lines] == ["auth", "user"], lines
        seqs = [int(l["text"].split("seq=")[1].split()[0]) for c in inbox.connections for l in c if l["type"] == "user"]
        assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs), f"push seqs must be strictly increasing: {seqs}"
        assert _health(port)["bridge"]["failures"] == 0
        clock.lap("h. chat.message -> inbox socket")

        # -- i. restart: nothing lives only in memory ---------------------------------------
        before = _shape(_get(port, "/api/snapshot"))
        before_seq = _health(port)["latest_seq"]
        canvas.close()
        canvas = None
        stopped = cli.stop()
        assert stopped["stopped"] is True and stopped["pid"] == info["pid"], stopped
        assert not (project / ".whiteboard" / "server.json").exists()
        _wait_for(lambda: _health(port) is None, timeout=5, what="old server to go away")
        assert str(project.resolve()) not in json.loads(registry.read_text()), "registry entry must be removed on shutdown"
        clock.lap("i1. whiteboard stop")

        info = cli.start()
        port = int(info["port"])
        canvas = Canvas(port)
        snapshot, _ = canvas.hello(client_id="e2e-after-restart")
        after = _shape(snapshot)
        assert after == before, f"state changed across restart:\n{json.dumps(before, indent=1)}\n---\n{json.dumps(after, indent=1)}"
        assert after["nodes"]["node-b"][0] == "B renamed" and after["nodes"]["node-a"][2] == "done"
        assert set(after["nodes"]["node-c"][4]) == {"node-a", "node-b"} and after["agents"]["agent-b"][2] == ["node-a"]
        assert _health(port)["latest_seq"] == before_seq
        replayed = [m for m in canvas.seen if m["type"] == "event.append"]
        assert len(replayed) == before_seq and replayed[-1]["seq"] == before_seq
        clock.lap("i2. whiteboard start + hello after restart")
        print("\n" + clock.report())
    finally:
        if canvas is not None:
            canvas.close()
        try:
            cli.run("stop", check=False, timeout=30)
        finally:
            leftover = info.get("pid")
            if leftover:
                try:
                    os.kill(int(leftover), 9)
                except (ProcessLookupError, PermissionError, ValueError):
                    pass
            inbox.close()
            shutil.rmtree(tmp, ignore_errors=True)
