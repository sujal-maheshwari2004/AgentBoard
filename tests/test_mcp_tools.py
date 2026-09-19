"""MCP tool surface (CONTRACTS §8) driven through the in-process mcp v2 Client."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from mcp.client import Client

from whiteboard.events.log import EventLog
from whiteboard.files.atomic import SelfWriteRegistry
from whiteboard.mcp_server import (
    EVENT_TYPES,
    MAX_RESULT_CHARS,
    WhiteboardToolError,
    build_mcp_server,
    guard_result,
    read_only_skeleton,
)
from whiteboard.plan.store import PlanStore
from whiteboard.scaffold import scaffold

CONTRACT_TOOLS = {
    "status", "read_plan", "read_node", "read_collaboration", "read_plan_md",
    "upsert_node", "set_node_status", "delete_node", "write_diagram",
    "upsert_agent", "set_agent_status", "append_event", "ask_user", "get_reply",
    "get_dispatchable", "propose_dispatch", "get_events", "wait_for_events",
    "get_skeleton", "read_peer_node", "save_layout",
}


class FakeBus:
    def __init__(self) -> None:
        self.published: list[tuple[str, dict, int | None]] = []
        self.pushed: list[str] = []

    def publish(self, type: str, payload: dict, *, seq: int | None = None) -> None:
        self.published.append((type, payload, seq))

    def push_root(self, lines: list[str]) -> None:
        self.pushed.extend(lines)

    def bridge_status(self) -> dict:
        return {"ok": True, "failures": 0}

    def types(self) -> list[str]:
        return [t for t, _, _ in self.published]

    def payloads(self, type: str) -> list[dict]:
        return [p for t, p, _ in self.published if t == type]


class Harness:
    """Real PlanStore + EventLog over a scaffolded project, a FakeBus, and the MCP server.

    Each call opens a fresh in-process ``Client`` (cheap; mirrors ``stateless_http``) -
    keeping one open across a pytest-asyncio fixture trips anyio's cancel-scope task check.
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        self.store = PlanStore(root, self_writes=SelfWriteRegistry())
        self.store.load()
        self.log = EventLog(root / ".whiteboard" / "events.jsonl")
        self.log.load()
        self.bus = FakeBus()
        self.server = build_mcp_server(self.store, self.log, self.bus, root)

    async def list_tools(self):
        async with Client(self.server) as c:
            return (await c.list_tools()).tools

    async def call(self, tool_name: str, /, **args) -> dict:
        async with Client(self.server) as c:
            r = await c.call_tool(tool_name, args)
        text = r.content[0].text if r.content else ""
        if r.is_error:
            raise ToolFailure(text)
        return json.loads(text)

    async def chain(self) -> None:
        """node-a <- node-b <- node-c (c depends on a and b)."""
        await self.call("upsert_node", id="node-a", title="A thing")
        await self.call("upsert_node", id="node-b", title="B thing", depends_on=["node-a"])
        await self.call(
            "upsert_node", id="node-c", title="C thing", type="lld",
            depends_on=["node-a", "node-b"], interfaces=["f() -> int"],
        )
        self.bus.published.clear()
        self.bus.pushed.clear()


class ToolFailure(Exception):
    pass


@pytest.fixture
def root(tmp_path: Path, monkeypatch) -> Path:
    # never touch the real ~/.whiteboard/registry.json
    monkeypatch.setenv("WHITEBOARD_REGISTRY", str(tmp_path / "registry.json"))
    project = tmp_path / "proj"
    project.mkdir()
    scaffold(project)
    return project


@pytest.fixture
def h(root: Path) -> Harness:
    return Harness(root)


def _wb(root: Path) -> Path:
    return root / ".whiteboard"


# ----------------------------------------------------------------- surface


async def test_tool_names_match_contract(h: Harness) -> None:
    tools = await h.list_tools()
    assert {t.name for t in tools} == CONTRACT_TOOLS
    assert h.server.name == "whiteboard"
    assert "append_event" in (h.server.instructions or "") and "ask_user" in h.server.instructions


async def test_tool_params_match_contract(h: Harness) -> None:
    tools = {t.name: t for t in await h.list_tools()}
    props = lambda n: set(tools[n].input_schema["properties"])  # noqa: E731
    assert props("upsert_node") == {"id", "title", "type", "status", "depends_on", "interfaces", "body", "diagram"}
    assert set(tools["upsert_node"].input_schema["required"]) == {"id", "title"}
    assert props("upsert_agent") == {
        "agent_id", "assigned_node", "status", "plan_md", "diagrams_md", "claude_agent_ref", "notes"
    }
    assert props("append_event") == {"agent_id", "node_id", "type", "note", "data"}
    assert props("ask_user") == {"agent_id", "question", "choices", "node_id", "kind"}
    assert props("propose_dispatch") == {"node_id", "agent_id", "job_spec_md"}
    assert props("wait_for_events") == {"since_seq", "timeout_s"}
    assert props("get_events") == {"since_seq", "limit"}
    assert props("status") == set()


async def test_status(h: Harness, root: Path) -> None:
    out = await h.call("status")
    assert out == {
        "project": root.name, "port": None, "rev": h.store.rev, "nodes": 0, "agents": 0,
        "latest_seq": 0, "bridge": {"ok": True, "failures": 0},
    }
    (_wb(root) / "server.json").write_text(json.dumps({"pid": 1, "port": 43217}))
    await h.call("upsert_node", id="node-a", title="A")
    out = await h.call("status")
    assert out["port"] == 43217 and out["nodes"] == 1 and out["latest_seq"] == 1


# ----------------------------------------------------------------- nodes


async def test_upsert_nodes_and_read_plan(h: Harness, root: Path) -> None:
    await h.chain()
    plan = await h.call("read_plan")
    assert "layout" not in plan
    assert [n["id"] for n in plan["nodes"]] == ["node-a", "node-b", "node-c"]
    assert {(e["src"], e["dst"]) for e in plan["edges"]} == {
        ("node-a", "node-b"), ("node-a", "node-c"), ("node-b", "node-c")
    }
    assert plan["project"] == root.name and plan["agents"] == []
    assert {d["name"] for d in plan["diagrams"]} == {"hld", "er"}
    assert (_wb(root) / "plan" / "nodes" / "node-c.md").exists()
    # each upsert published the store's messages and appended a node_changed event
    events = await h.call("get_events")
    assert [e["type"] for e in events["events"]] == ["node_changed"] * 3
    assert all(e["agent_id"] == "root" for e in events["events"])
    assert events["events"][0]["note"] == "created node-a (A thing)"

    node = await h.call("read_node", id="node-c")
    assert node["type"] == "lld" and node["depends_on"] == ["node-a", "node-b"]
    assert node["interfaces"] == ["f() -> int"]


async def test_upsert_publishes_node_and_edge_messages(h: Harness) -> None:
    await h.call("upsert_node", id="node-a", title="A")
    h.bus.published.clear()
    await h.call("upsert_node", id="node-b", title="B", depends_on=["node-a"])
    types = h.bus.types()
    assert "plan.node.upsert" in types and "plan.edge.upsert" in types
    assert types[-1] == "event.append"
    ev_type, ev_payload, ev_seq = h.bus.published[-1]
    assert ev_payload["type"] == "node_changed" and ev_seq == ev_payload["seq"] == 2


async def test_set_node_status(h: Harness) -> None:
    await h.chain()
    node = await h.call("set_node_status", id="node-a", status="in_progress")
    assert node["status"] == "in_progress" and node["owner"] is None
    await h.call("upsert_agent", agent_id="agent-a", assigned_node="node-a")
    node = await h.call("set_node_status", id="node-a", status="done", owner="agent-a")
    assert node["status"] == "done" and node["owner"] == "agent-a"
    assert h.store.get_node("node-a").status == "done"
    ev = (await h.call("get_events"))["events"][-1]
    assert ev["type"] == "node_changed" and "done" in ev["note"] and ev["node_id"] == "node-a"


async def test_delete_node(h: Harness, root: Path) -> None:
    await h.chain()
    out = await h.call("delete_node", id="node-b")
    assert out == {"deleted": "node-b"}
    assert not (_wb(root) / "plan" / "nodes" / "node-b.md").exists()
    assert h.store.get_node("node-c").depends_on == ["node-a"]
    assert "plan.node.delete" in h.bus.types()
    with pytest.raises(ToolFailure, match="unknown node"):
        await h.call("delete_node", id="node-b")


async def test_write_diagram_creates_nodes(h: Harness, root: Path) -> None:
    mermaid = "flowchart LR\n    node-x[X box]\n    node-y[Y box]\n    node-x --> node-y\n"
    diagram = await h.call("write_diagram", name="hld", mermaid=mermaid)
    assert diagram["name"] == "hld" and diagram["direction"] == "LR"
    assert [(e["src"], e["dst"]) for e in diagram["edges"]] == [("node-x", "node-y")]
    assert set(h.store.nodes) == {"node-x", "node-y"}
    assert h.store.get_node("node-y").depends_on == ["node-x"]
    assert h.store.get_node("node-x").title == "X box"
    assert (_wb(root) / "plan" / "nodes" / "node-y.md").exists()
    ev = (await h.call("get_events"))["events"][-1]
    assert ev["type"] == "node_changed" and set(ev["data"]["created"]) == {"node-x", "node-y"}
    with pytest.raises(ToolFailure, match="invalid mermaid"):
        await h.call("write_diagram", name="hld", mermaid="flowchart TD\n    node-a --> \n")


# ----------------------------------------------------------------- agents


async def test_upsert_agent_and_set_status(h: Harness, root: Path) -> None:
    await h.chain()
    await h.call("set_node_status", id="node-a", status="done")
    card = await h.call(
        "upsert_agent", agent_id="agent-b", assigned_node="node-b", plan_md="# spec", notes="hi",
    )
    assert card["id"] == "agent-b" and card["status"] == "idle" and card["ready_deps"] == ["node-a"]
    assert card["plan_md"] == "# spec"
    assert (_wb(root) / "agents" / "agent-b" / "plan.md").read_text() == "# spec"
    assert (_wb(root) / "agents" / "agent-b" / "card.md").exists()
    assert "agent.card.upsert" in h.bus.types()

    card = await h.call("set_agent_status", agent_id="agent-b", status="working", note="started")
    assert card["status"] == "working"
    ev = (await h.call("get_events"))["events"][-1]
    assert ev["type"] == "agent_changed" and ev["agent_id"] == "agent-b" and ev["node_id"] == "node-b"
    assert "started" in ev["note"]

    card = await h.call("upsert_agent", agent_id="agent-b", assigned_node="node-b", claude_agent_ref="ref-1")
    assert card["claude_agent_ref"] == "ref-1" and card["status"] == "idle"


async def test_upsert_agent_requires_existing_node(h: Harness) -> None:
    with pytest.raises(ToolFailure, match="unknown node"):
        await h.call("upsert_agent", agent_id="agent-x", assigned_node="node-nope")
    with pytest.raises(ToolFailure, match="unknown agent"):
        await h.call("set_agent_status", agent_id="agent-x", status="working")


# ----------------------------------------------------------------- events


async def test_append_event_done_bookkeeping(h: Harness) -> None:
    await h.chain()
    await h.call("upsert_agent", agent_id="agent-a", assigned_node="node-a", status="working")
    await h.call("upsert_agent", agent_id="agent-b", assigned_node="node-b")
    await h.call("upsert_agent", agent_id="agent-c", assigned_node="node-c")
    await h.call("set_node_status", id="node-a", owner="agent-a", status="in_progress")
    h.bus.published.clear()
    h.bus.pushed.clear()

    ev = await h.call("append_event", agent_id="agent-a", node_id="node-a", type="done", note="landed")
    assert ev["type"] == "done" and ev["note"] == "landed" and ev["agent_id"] == "agent-a"
    assert ev["notified"] == ["agent-b", "agent-c"]
    assert ev["data"]["dispatchable"] == ["node-b"]

    assert h.store.get_node("node-a").status == "done"
    assert h.store.get_agent("agent-a").status == "done"
    assert h.store.get_agent("agent-b").ready_deps == ["node-a"]
    assert h.store.get_agent("agent-c").ready_deps == ["node-a"]

    types = h.bus.types()
    assert "plan.node.upsert" in types
    assert types.count("agent.card.upsert") == 3  # agent-a done + two ready_deps updates
    assert types[-1] == "event.append" and h.bus.published[-1][2] == ev["seq"]
    upserted = {p["agent"]["id"] for p in h.bus.payloads("agent.card.upsert")}
    assert upserted == {"agent-a", "agent-b", "agent-c"}

    assert h.bus.pushed == ["agent-a done on node-a; notified agent-b, agent-c; now dispatchable: node-b"]


async def test_append_event_done_without_dependents(h: Harness) -> None:
    await h.call("upsert_node", id="node-solo", title="Solo")
    h.bus.pushed.clear()
    ev = await h.call("append_event", agent_id="agent-solo", node_id="node-solo", type="done")
    assert ev["notified"] == []
    assert h.bus.pushed == ["agent-solo done on node-solo; notified nobody; now dispatchable: nothing new"]


async def test_append_event_blocked(h: Harness) -> None:
    await h.chain()
    await h.call("upsert_agent", agent_id="agent-b", assigned_node="node-b", status="working")
    h.bus.pushed.clear()
    ev = await h.call("append_event", agent_id="agent-b", node_id="node-b", type="blocked", note="need creds")
    assert ev["type"] == "blocked"
    assert h.store.get_agent("agent-b").status == "blocked"
    assert h.store.get_node("node-b").status == "blocked"
    assert h.bus.pushed == ["agent-b blocked on node-b: need creds"]
    # an agent without a card still blocks the node
    await h.call("append_event", agent_id="agent-ghost", node_id="node-c", type="blocked", note="x")
    assert h.store.get_node("node-c").status == "blocked"


async def test_append_event_needs_input_creates_prompt(h: Harness) -> None:
    await h.chain()
    ev = await h.call(
        "append_event", agent_id="agent-c", node_id="node-c", type="needs_input",
        note="which db?", data={"kind": "choice", "choices": ["pg", "sqlite"]},
    )
    pid = ev["data"]["prompt_id"]
    prompt = h.log.get_prompt(pid)
    assert prompt["question"] == "which db?" and prompt["kind"] == "choice"
    assert prompt["choices"] == ["pg", "sqlite"] and prompt["answered"] is False
    assert ev["type"] == "needs_input" and ev["note"] == "which db?"
    assert h.bus.payloads("needs_input") == [{
        "prompt_id": pid, "agent_id": "agent-c", "node_id": "node-c",
        "question": "which db?", "kind": "choice", "choices": ["pg", "sqlite"],
    }]
    assert h.bus.pushed[-1].startswith(f"needs_input (prompt {pid}, agent-c on node-c)")
    assert h.log.latest_seq == ev["seq"]  # exactly one event appended


async def test_append_event_generic_type(h: Harness) -> None:
    ev = await h.call("append_event", agent_id="root", node_id=None, type="chat", note="hello", data={"to": "agent-a"})
    assert ev["type"] == "chat" and ev["node_id"] is None and ev["data"] == {"to": "agent-a"}
    assert h.bus.pushed == ["chat (to: agent-a) from root: hello"]
    ev = await h.call("append_event", agent_id="agent-q", node_id=None, type="info", note="fyi")
    assert h.bus.pushed[-1] == "info from agent-q: fyi"


async def test_append_event_rejects_bad_input(h: Harness) -> None:
    await h.call("upsert_node", id="node-a", title="A")
    with pytest.raises(ToolFailure, match="invalid event type"):
        await h.call("append_event", agent_id="agent-a", node_id="node-a", type="finished")
    with pytest.raises(ToolFailure, match="unknown node"):
        await h.call("append_event", agent_id="agent-a", node_id="node-zzz", type="done")
    with pytest.raises(ToolFailure, match="invalid agent id"):
        await h.call("append_event", agent_id="Agent A", node_id="node-a", type="info")
    assert h.log.latest_seq == 1  # only the node_changed from upsert


async def test_ask_user_get_reply_round_trip(h: Harness) -> None:
    await h.call("upsert_node", id="node-a", title="A")
    h.bus.published.clear()
    h.bus.pushed.clear()
    out = await h.call("ask_user", agent_id="agent-a", question="ship it?", kind="confirm", node_id="node-a")
    pid = out["prompt_id"]
    assert len(pid) == 8
    assert h.bus.types() == ["event.append", "needs_input"]
    assert h.bus.payloads("needs_input")[0] == {
        "prompt_id": pid, "agent_id": "agent-a", "node_id": "node-a",
        "question": "ship it?", "kind": "confirm", "choices": [],
    }
    assert h.bus.pushed == [f'needs_input (prompt {pid}, agent-a on node-a): "ship it?"']

    assert await h.call("get_reply", prompt_id=pid) == {"answered": False}
    h.log.answer_prompt(pid, True)
    assert await h.call("get_reply", prompt_id=pid) == {"answered": True, "value": True}
    with pytest.raises(ToolFailure, match="unknown prompt_id"):
        await h.call("get_reply", prompt_id="nope1234")
    with pytest.raises(ToolFailure, match="invalid prompt kind"):
        await h.call("ask_user", agent_id="agent-a", question="?", kind="essay")
    with pytest.raises(ToolFailure, match="must not be empty"):
        await h.call("ask_user", agent_id="agent-a", question="  ")


# ----------------------------------------------------------------- dispatch


async def test_get_dispatchable(h: Harness) -> None:
    await h.chain()
    out = await h.call("get_dispatchable")
    assert [n["id"] for n in out["nodes"]] == ["node-a"]
    await h.call("set_node_status", id="node-a", status="done")
    assert [n["id"] for n in (await h.call("get_dispatchable"))["nodes"]] == ["node-b"]
    await h.call("set_node_status", id="node-b", status="todo", owner="agent-b")
    assert (await h.call("get_dispatchable"))["nodes"] == []


async def test_propose_dispatch_with_spec(h: Harness, root: Path) -> None:
    await h.chain()
    out = await h.call("propose_dispatch", node_id="node-a", agent_id="agent-a", job_spec_md="# my spec\n")
    assert len(out["request_id"]) == 8 and out["job_spec_md"] == "# my spec\n"
    assert (_wb(root) / "agents" / "agent-a" / "plan.md").read_text() == "# my spec\n"
    card = h.store.get_agent("agent-a")
    assert card.status == "idle" and card.assigned_node == "node-a"
    assert h.bus.payloads("dispatch.request") == [{
        "request_id": out["request_id"], "node_id": "node-a", "agent_id": "agent-a", "job_spec_md": "# my spec\n",
    }]
    assert "agent.card.upsert" in h.bus.types()
    ev = (await h.call("get_events"))["events"][-1]
    assert ev["type"] == "dispatch_proposed"
    assert ev["data"] == {"request_id": out["request_id"], "node_id": "node-a", "agent_id": "agent-a"}
    assert h.bus.published[-2][0] == "event.append"  # the event was broadcast before dispatch.request


async def test_propose_dispatch_generates_spec(h: Harness, root: Path) -> None:
    await h.chain()
    out = await h.call("propose_dispatch", node_id="node-b", agent_id="agent-b", job_spec_md="")
    spec = out["job_spec_md"]
    assert spec.startswith("# Job spec: B thing")
    assert "`node-a` — A thing" in spec  # consumed interface
    assert "`node-c` — C thing" in spec  # dependent
    assert str(root / ".whiteboard" / "COLLABORATION.md") in spec
    assert (_wb(root) / "agents" / "agent-b" / "plan.md").read_text() == spec
    out2 = await h.call("propose_dispatch", node_id="node-b", agent_id="agent-b")
    assert out2["job_spec_md"] == spec and out2["request_id"] != out["request_id"]
    with pytest.raises(ToolFailure, match="unknown node"):
        await h.call("propose_dispatch", node_id="node-zzz", agent_id="agent-z", job_spec_md="x")


# ----------------------------------------------------------------- event reads


async def test_get_events(h: Harness) -> None:
    await h.chain()
    out = await h.call("get_events")
    assert out["latest_seq"] == 3 and [e["seq"] for e in out["events"]] == [1, 2, 3]
    out = await h.call("get_events", since_seq=2)
    assert [e["seq"] for e in out["events"]] == [3]
    out = await h.call("get_events", since_seq=0, limit=2)
    assert [e["seq"] for e in out["events"]] == [1, 2] and out["latest_seq"] == 3
    with pytest.raises(ToolFailure, match=">= 0"):
        await h.call("get_events", since_seq=-1)


async def test_wait_for_events_returns_immediately(h: Harness) -> None:
    await h.chain()
    t0 = asyncio.get_running_loop().time()
    out = await h.call("wait_for_events", since_seq=1, timeout_s=10)
    assert asyncio.get_running_loop().time() - t0 < 1
    assert [e["seq"] for e in out["events"]] == [2, 3] and out["latest_seq"] == 3


async def test_wait_for_events_wakes_on_append(h: Harness) -> None:
    await h.chain()

    async def later() -> None:
        await asyncio.sleep(0.1)
        h.log.append(agent_id="agent-x", node_id=None, type="info", note="ping")

    task = asyncio.create_task(later())
    t0 = asyncio.get_running_loop().time()
    out = await h.call("wait_for_events", since_seq=3, timeout_s=10)
    await task
    assert asyncio.get_running_loop().time() - t0 < 5
    assert [e["type"] for e in out["events"]] == ["info"] and out["latest_seq"] == 4
    assert h.log._subscribers == []  # unsubscribed


async def test_wait_for_events_times_out_empty(h: Harness) -> None:
    await h.chain()
    t0 = asyncio.get_running_loop().time()
    out = await h.call("wait_for_events", since_seq=3, timeout_s=0.2)
    elapsed = asyncio.get_running_loop().time() - t0
    assert 0.15 <= elapsed < 3
    assert out == {"events": [], "latest_seq": 3}
    assert h.log._subscribers == []


# ----------------------------------------------------------------- cross-project


async def test_get_skeleton_local(h: Harness, root: Path) -> None:
    await h.chain()
    skel = await h.call("get_skeleton")
    assert skel["project"] == root.name
    assert [n["id"] for n in skel["nodes"]] == ["node-a", "node-b", "node-c"]
    assert skel["nodes"][1] == {"id": "node-b", "title": "B thing", "type": "hld", "status": "todo", "depends_on": ["node-a"]}
    assert await h.call("get_skeleton", project_path=str(root)) == skel


async def test_get_skeleton_peer_file_fallback(h: Harness, tmp_path: Path) -> None:
    peer = tmp_path / "peer"
    peer.mkdir()
    scaffold(peer)
    other = PlanStore(peer, self_writes=SelfWriteRegistry())
    other.load()
    other.upsert_node("node-p", title="Peer node", status="done")
    other.upsert_node("node-q", title="Q", depends_on=["node-p"], body="secret body")
    plan_md_before = (peer / ".whiteboard" / "PLAN.md").read_bytes()

    skel = await h.call("get_skeleton", project_path=str(peer))
    assert skel["project"] == "peer"
    assert [n["id"] for n in skel["nodes"]] == ["node-p", "node-q"]
    assert skel["nodes"][0]["status"] == "done" and skel["nodes"][1]["depends_on"] == ["node-p"]
    assert (peer / ".whiteboard" / "PLAN.md").read_bytes() == plan_md_before  # read-only
    # a project with no whiteboard state yields an empty skeleton
    empty = tmp_path / "empty"
    empty.mkdir()
    assert await h.call("get_skeleton", project_path=str(empty)) == {"project": "empty", "nodes": []}


async def test_read_peer_node(h: Harness, tmp_path: Path) -> None:
    peer = tmp_path / "peer2"
    peer.mkdir()
    scaffold(peer)
    other = PlanStore(peer, self_writes=SelfWriteRegistry())
    other.load()
    other.upsert_node("node-p", title="Peer node", type="er")
    node = await h.call("read_peer_node", project_path=str(peer), node_id="node-p")
    assert node["id"] == "node-p" and node["title"] == "Peer node" and node["type"] == "er"
    with pytest.raises(ToolFailure, match="not found"):
        await h.call("read_peer_node", project_path=str(peer), node_id="node-zz")
    await h.call("upsert_node", id="node-a", title="Local", body="local body")
    local = await h.call("read_peer_node", project_path=str(h.root), node_id="node-a")
    assert local["body"] == "local body"


def test_read_only_skeleton_skips_invalid(tmp_path: Path) -> None:
    nodes = tmp_path / ".whiteboard" / "plan" / "nodes"
    nodes.mkdir(parents=True)
    (nodes / "node-ok.md").write_text("---\nid: node-ok\ntitle: OK\n---\n\nbody\n")
    (nodes / "bad id.md").write_text("---\nid: Bad Id\n---\n")
    (nodes / ".hidden.md").write_text("---\nid: node-hidden\n---\n")
    skel = read_only_skeleton(tmp_path)
    assert [n["id"] for n in skel.nodes] == ["node-ok"]


# ----------------------------------------------------------------- misc


async def test_read_collaboration_and_plan_md(h: Harness, root: Path) -> None:
    (_wb(root) / "COLLABORATION.md").write_text("# COLLAB\nbe nice\n")
    assert (await h.call("read_collaboration"))["text"] == "# COLLAB\nbe nice\n"
    await h.call("upsert_node", id="node-a", title="A thing")
    text = (await h.call("read_plan_md"))["text"]
    assert text.startswith("# PLAN") and "node-a" in text


async def test_save_layout(h: Harness, root: Path) -> None:
    await h.call("upsert_node", id="node-a", title="A")
    out = await h.call("save_layout", diagram="hld", patch={"nodes": {"node-a": {"x": 1, "y": 2}}})
    assert out == {"ok": True}
    layout = json.loads((_wb(root) / "plan" / "hld.layout.json").read_text())
    assert layout["nodes"]["node-a"]["x"] == 1
    assert h.bus.payloads("layout.update")[0]["diagram"] == "hld"
    with pytest.raises(ToolFailure, match="invalid diagram name"):
        await h.call("save_layout", diagram="Bad Name", patch={})


async def test_invalid_ids_raise_with_message(h: Harness) -> None:
    with pytest.raises(ToolFailure, match="invalid node id"):
        await h.call("upsert_node", id="Node A", title="x")
    with pytest.raises(ToolFailure, match="unknown node"):
        await h.call("read_node", id="node-missing")
    with pytest.raises(ToolFailure, match="invalid agent id"):
        await h.call("upsert_agent", agent_id="bob", assigned_node="node-a")
    with pytest.raises(ToolFailure, match="invalid status"):
        await h.call("upsert_node", id="node-a", title="x", status="paused")
    with pytest.raises(ToolFailure, match="invalid type"):
        await h.call("upsert_node", id="node-a", title="x", type="ddd")
    # wrong argument types are rejected by the SDK's schema validation
    with pytest.raises(ToolFailure, match="validation error"):
        await h.call("get_events", since_seq="zero")
    assert h.store.nodes == {} and h.log.latest_seq == 0


def test_error_class_is_valueerror() -> None:
    assert issubclass(WhiteboardToolError, ValueError)
    assert set(EVENT_TYPES) >= {"done", "blocked", "needs_input", "reply", "chat", "info"}


async def test_size_guard_truncates_large_bodies(h: Harness) -> None:
    big = "x" * (MAX_RESULT_CHARS // 2)
    await h.call("upsert_node", id="node-big1", title="big 1", body=big)
    await h.call("upsert_node", id="node-big2", title="big 2", body=big)
    plan = await h.call("read_plan")
    assert plan["truncated"] is True
    assert all(len(n["body"]) < 3000 and n["body"].endswith("[truncated]") for n in plan["nodes"])
    assert len(json.dumps(plan)) < MAX_RESULT_CHARS
    small = await h.call("read_node", id="node-big1")
    assert "truncated" not in small and len(small["body"]) == len(big)


def test_guard_result_leaves_small_results_alone() -> None:
    r = {"body": "x" * 5000, "nested": [{"plan_md": "y" * 5000}]}
    assert guard_result(r) is r
    huge = {"nodes": [{"body": "z" * (MAX_RESULT_CHARS + 1)}], "text": "t" * 10}
    out = guard_result(huge)
    assert out["truncated"] is True and out["text"] == "t" * 10
    assert out["nodes"][0]["body"].endswith("[truncated]")
