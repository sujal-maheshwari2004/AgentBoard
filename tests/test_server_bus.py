"""`Bus` fan-out rules that no handler goes through: the chat projection
(CONTRACTS §7) and the two kinds of external (on-disk) edit (§1 events)."""

from __future__ import annotations

from pathlib import Path

import pytest

from whiteboard.server.bus import ServerContext


@pytest.fixture
def ctx(project: Path) -> ServerContext:
    c = ServerContext(project)
    c.load()
    return c


@pytest.fixture(autouse=True)
def _record_broadcasts(monkeypatch) -> None:
    """Record ``hub.broadcast`` instead of delivering (no event loop in these tests)."""
    from whiteboard.server.hub import Hub

    def broadcast(self, envelope, exclude=None):  # noqa: ANN001
        store = getattr(self, "_recorded", None)
        if store is None:
            store = self._recorded = []
        store.append(envelope)
        return 0

    monkeypatch.setattr(Hub, "broadcast", broadcast)


def _frames(ctx: ServerContext) -> list[tuple[str, dict]]:
    return [(e["type"], e["payload"]) for e in getattr(ctx.hub, "_recorded", [])]


# ------------------------------------------------------------------- chat projection
def test_publish_projects_each_chat_event_exactly_once(ctx: ServerContext) -> None:
    ev = ctx.log.append(agent_id="agent-x", node_id=None, type="chat", note="on it",
                        data={"from": "agent-x", "to": "user", "thread": "agent-x"})
    # the producer publishes the event itself...
    ctx.bus.publish_event(ev)
    # ...and the event pump publishes the same event again: the seq dedupe drops it,
    # so the canvas sees one chat.message, not two.
    ctx.bus.publish_event(ev)
    assert _frames(ctx) == [
        ("event.append", ev.model_dump()),
        ("chat.message", {"id": f"chat-{ev.seq}", "thread": "agent-x", "from": "agent-x",
                          "to": "user", "text": "on it", "ts": ev.ts, "seq": ev.seq,
                          "reply_to": None, "node_id": None}),
    ]


def test_publish_ignores_non_chat_events(ctx: ServerContext) -> None:
    ev = ctx.log.append(agent_id="root", node_id=None, type="info", note="hello")
    ctx.bus.publish_event(ev)
    assert [t for t, _ in _frames(ctx)] == ["event.append"]


# ------------------------------------------------------------------- external edits
def test_external_to_event_agent_kind(ctx: ServerContext) -> None:
    """An agent's plan.md/diagrams.md edited on disk is a message to that agent."""
    payload = {
        "summary": "agent plan edited: agent-a — Use the fixture corpus",
        "risky": False, "kind": "agent", "ids": ["agent-a"], "field": "plan_md",
        "path": ".whiteboard/agents/agent-a/plan.md",
        "diff": "--- a/plan.md\n+++ b/plan.md\n+Use the fixture corpus\n",
    }
    ev = ctx.bus.external_to_event(payload)
    assert ev is not None and ev.type == "agent_plan_edited" and ev.agent_id == "user"
    assert ev.notified == ["agent-a"] and ev.note == payload["summary"]
    assert ev.data["source"] == "file" and ev.data["agent_id"] == "agent-a"
    assert ev.data["field"] == "plan_md" and ev.data["diff"] == payload["diff"]
    assert ev.data["kind"] == "agent" and ev.data["ids"] == ["agent-a"]
    # pushed verbatim: no "external edit:" prefix and no "[path]" suffix
    assert list(ctx.bridge._lines) == [payload["summary"]]


def test_external_to_event_node_kind_is_unchanged(ctx: ServerContext) -> None:
    ev = ctx.bus.external_to_event({
        "summary": "node-c: renamed to 'C edited'", "risky": False, "kind": "node",
        "ids": ["node-c"], "path": ".whiteboard/plan/nodes/node-c.md",
    })
    assert ev is not None and ev.type == "node_changed" and ev.node_id == "node-c"
    assert list(ctx.bridge._lines) == [
        "external edit: node-c: renamed to 'C edited' [.whiteboard/plan/nodes/node-c.md]"
    ]


def test_external_agent_card_error_keeps_the_generic_path(ctx: ServerContext) -> None:
    """A broken card.md has no ``field``: it is a plan problem, not an instruction."""
    ev = ctx.bus.external_to_event({
        "summary": "invalid agent card: boom", "risky": False, "kind": "agent",
        "ids": ["agent-a"], "path": ".whiteboard/agents/agent-a/card.md", "error": "boom",
    })
    assert ev is not None and ev.type == "node_changed"
    assert list(ctx.bridge._lines)[0].startswith("external edit: invalid agent card")


# ------------------------------------------------------------------- pending diagrams
def test_load_rebuilds_pending_diagrams_from_the_log(ctx: ServerContext) -> None:
    mermaid = "flowchart TD\n    node-a[A thing]\n"
    for rid in ("p-1", "p-2", "p-3"):
        ctx.log.append(agent_id="root", node_id=None, type="diagram_proposed", note="propose",
                       data={"request_id": rid, "name": "lld", "mermaid": mermaid, "rationale": "why"})
    ctx.log.append(agent_id="user", node_id=None, type="diagram_approved", note="",
                   data={"request_id": "p-1"})
    ctx.log.append(agent_id="user", node_id=None, type="diagram_rejected", note="",
                   data={"request_id": "p-2"})
    pending = ctx.restore_pending_diagrams()
    assert set(pending) == {"p-3"}
    assert pending["p-3"]["name"] == "lld" and pending["p-3"]["mermaid"] == mermaid
    assert pending["p-3"]["rationale"] == "why" and pending["p-3"]["agent_id"] == "root"
    assert ctx.health()["pending_diagrams"] == 1


# ------------------------------------------------------------------- health
def test_health_lists_agents(ctx: ServerContext) -> None:
    ctx.store.upsert_agent("agent-b", "node-b")
    ctx.store.upsert_agent("agent-a", "node-a")
    ctx.store.touch_agent("agent-a", activity="writing tests", progress=0.5,
                          metrics={"tool_calls": 3}, files_touched=["a.py"])
    body = ctx.health()
    assert body["agent_count"] == 2
    assert [a["id"] for a in body["agents"]] == ["agent-a", "agent-b"]
    a, b = body["agents"]
    assert a["activity"] == "writing tests" and a["progress"] == 0.5
    assert a["metrics"] == {"tool_calls": 3, "files_touched": ["a.py"]}
    assert a["assigned_node"] == "node-a" and a["status"] == "idle" and a["heartbeat_at"]
    assert a["finished_at"] is None and a["spawned_at"] is None
    assert b["activity"] is None and b["metrics"] == {} and b["heartbeat_at"] is None
