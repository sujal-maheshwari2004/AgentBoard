"""Websocket flows through the real app (lifespan on) — CONTRACTS §6 / §9."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from tests.conftest import hello, recv_until
from whiteboard.server.app import create_app


@pytest.fixture
def client(project: Path):
    app = create_app(project)
    with TestClient(app) as c:
        c.app_ref = app  # type: ignore[attr-defined]
        yield c


def _state(client: TestClient):
    return client.app_ref.state  # type: ignore[attr-defined]


def _events(client: TestClient, since: int = 0) -> list[dict]:
    return client.get(f"/api/events?since={since}").json()["events"]


def _bridge_lines(client: TestClient) -> list[str]:
    return list(_state(client).bridge._lines)


def _node_file(project: Path, node_id: str) -> Path:
    return project / ".whiteboard" / "plan" / "nodes" / f"{node_id}.md"


# ------------------------------------------------------------------- hello
def test_hello_returns_snapshot_backfill_and_bridge_status(client: TestClient) -> None:
    log = _state(client).log
    log.append(agent_id="root", node_id=None, type="info", note="one")
    log.append(agent_id="root", node_id=None, type="info", note="two")
    with client.websocket_connect("/ws") as ws:
        snap, rest = hello(ws, last_seq=1)
        assert snap["seq"] == 1_000_000 and snap["replyTo"] == 1
        assert {n["id"] for n in snap["payload"]["nodes"]} == {"node-a", "node-b", "node-c"}
        assert snap["payload"]["project"] == "proj" and "layout" in snap["payload"]
        assert [m["type"] for m in rest] == ["event.append", "bridge.status"]
        assert rest[0]["seq"] == 2 and rest[0]["payload"]["note"] == "two"
        assert rest[1]["payload"]["ok"] is False and rest[1]["payload"]["failures"] == 0
        assert rest[1]["seq"] >= 1_000_000
    assert client.get("/api/health").json()["clients"] == 0


def test_unknown_and_malformed_messages_yield_server_error(client: TestClient) -> None:
    with client.websocket_connect("/ws") as ws:
        hello(ws)
        ws.send_json({"type": "bogus", "payload": {}, "seq": 5})
        m = ws.receive_json()
        assert m["type"] == "server.error"
        assert m["payload"] == {"code": "unknown_type", "message": "unknown message type 'bogus'", "forSeq": 5}
        assert m["replyTo"] == 5
        ws.send_text("{not json")
        m = ws.receive_json()
        assert m["type"] == "server.error" and m["payload"]["code"] == "bad_json"
        ws.send_json({"type": "node.status", "payload": {"id": "node-a"}, "seq": 6})
        m = ws.receive_json()
        assert m["payload"]["code"] == "bad_payload" and m["payload"]["forSeq"] == 6


# ------------------------------------------------------------------- canvas.edit
def test_rename_edit_acks_upserts_writes_file_logs_and_pushes(client: TestClient, project: Path) -> None:
    with client.websocket_connect("/ws") as ws:
        hello(ws)
        ws.send_json({"type": "canvas.edit", "payload": {"ops": [{"op": "renamed", "id": "node-a", "label": "A renamed"}]}, "seq": 2})
        ack, seen = recv_until(ws, "edit.ack")
        upsert = next(m for m in seen if m["type"] == "plan.node.upsert")
        assert upsert["payload"]["node"]["title"] == "A renamed"
        assert ack["payload"]["forSeq"] == 2 and ack["replyTo"] == 2
        assert ack["payload"]["rev"] == _state(client).store.rev
        ev, _ = recv_until(ws, "event.append")
        assert ev["seq"] == 1 and ev["payload"]["type"] == "node_changed" and ev["payload"]["agent_id"] == "user"
        assert ev["payload"]["node_id"] == "node-a" and 'renamed to "A renamed"' in ev["payload"]["note"]
    assert "title: A renamed" in _node_file(project, "node-a").read_text()
    assert _bridge_lines(client) == ['edit: node-a renamed to "A renamed"']


def test_edge_op_parks_as_risky_and_approval_commits(client: TestClient, project: Path) -> None:
    with client.websocket_connect("/ws") as ws:
        hello(ws)
        ws.send_json({"type": "canvas.edit", "payload": {"ops": [{"op": "edge-created", "from": "node-c", "to": "node-a", "diagram": "hld"}]}, "seq": 7})
        req, seen = recv_until(ws, "risky_edit.request")
        assert [m["type"] for m in seen] == ["risky_edit.request"]
        p = req["payload"]
        assert p["affected"] == ["node-a"] and p["summary"] == "node-a: depends_on +node-c"
        assert "+- node-c" in p["diff"] and req["replyTo"] == 7
        rid = p["request_id"]
        pending_ev, _ = recv_until(ws, "event.append")
        assert pending_ev["payload"]["type"] == "risky_edit" and pending_ev["payload"]["data"]["request_id"] == rid
        assert client.get("/api/debug/state").json()["pending_edits"][rid]["affected"] == ["node-a"]
        assert "depends_on: []" in _node_file(project, "node-a").read_text()  # nothing committed yet

        ws.send_json({"type": "risky_edit.reply", "payload": {"request_id": rid, "approved": True, "note": "fine"}, "seq": 8})
        ack, seen = recv_until(ws, "edit.ack")
        types = [m["type"] for m in seen]
        assert "plan.node.upsert" in types and "plan.edge.upsert" in types
        edge = next(m for m in seen if m["type"] == "plan.edge.upsert")["payload"]["edge"]
        assert (edge["src"], edge["dst"], edge["diagram"]) == ("node-c", "node-a", "hld")
        assert ack["payload"]["forSeq"] == 7  # the original edit's seq
        ev, _ = recv_until(ws, "event.append")
        assert ev["payload"]["type"] == "risky_edit_accepted"
        assert ev["payload"]["note"] == 'node-a: depends_on +node-c — "fine"'
    text = _node_file(project, "node-a").read_text()
    assert "- node-c" in text
    assert "node-c --> node-a" in (project / ".whiteboard" / "plan" / "hld.md").read_text()
    assert _bridge_lines(client) == [f'risky edit accepted (req {rid}): node-a: depends_on +node-c — "fine"']
    assert [e["type"] for e in _events(client)] == ["risky_edit", "risky_edit_accepted"]


def test_risky_rejection_sends_reject_with_revert(client: TestClient, project: Path) -> None:
    with client.websocket_connect("/ws") as ws:
        hello(ws)
        op = {"op": "deleted", "id": "node-c"}
        ws.send_json({"type": "canvas.edit", "payload": {"ops": [op]}, "seq": 3})
        req, _ = recv_until(ws, "risky_edit.request")
        rid = req["payload"]["request_id"]
        ws.send_json({"type": "risky_edit.reply", "payload": {"request_id": rid, "approved": False, "note": "keep it"}, "seq": 4})
        rej, seen = recv_until(ws, "edit.reject")
        assert rej["payload"]["forSeq"] == 3 and rej["payload"]["revert"] == [op]
        assert "rejected by user" in rej["payload"]["reason"]
        assert not any(m["type"].startswith("plan.") for m in seen)
        ev, _ = recv_until(ws, "event.append")
        assert ev["payload"]["type"] == "risky_edit_rejected"
        # unknown request id
        ws.send_json({"type": "risky_edit.reply", "payload": {"request_id": "nope", "approved": True, "note": ""}, "seq": 5})
        err, _ = recv_until(ws, "server.error")
        assert err["payload"]["code"] == "unknown_request"
    assert _node_file(project, "node-c").exists()
    assert _state(client).store.get_node("node-c") is not None


def test_invalid_edit_is_rejected_immediately(client: TestClient) -> None:
    with client.websocket_connect("/ws") as ws:
        hello(ws)
        ws.send_json({"type": "canvas.edit", "payload": {"ops": [{"op": "renamed", "id": "node-zzz", "label": "x"}]}, "seq": 2})
        rej, seen = recv_until(ws, "edit.reject")
        assert [m["type"] for m in seen] == ["edit.reject"]
        assert "unknown node" in rej["payload"]["reason"] and rej["payload"]["forSeq"] == 2
        ws.send_json({"type": "canvas.edit", "payload": {"ops": []}, "seq": 3})
        err, _ = recv_until(ws, "server.error")
        assert err["payload"]["code"] == "bad_payload"
    assert _events(client) == []


def test_node_created_op_creates_file(client: TestClient, project: Path) -> None:
    with client.websocket_connect("/ws") as ws:
        hello(ws)
        ws.send_json({"type": "canvas.edit", "payload": {"ops": [{"op": "node-created", "id": "node-new", "label": "New box", "shape": "rect", "diagram": "hld"}]}, "seq": 2})
        recv_until(ws, "edit.ack")
    assert _node_file(project, "node-new").exists()
    assert _bridge_lines(client) == ['edit: node-new created ("New box") in hld']


# ------------------------------------------------------------------- node.status
def test_node_status_button(client: TestClient, project: Path) -> None:
    with client.websocket_connect("/ws") as ws:
        hello(ws)
        ws.send_json({"type": "node.status", "payload": {"id": "node-a", "status": "done"}, "seq": 2})
        ack, seen = recv_until(ws, "edit.ack")
        up = next(m for m in seen if m["type"] == "plan.node.upsert")
        assert up["payload"]["node"]["status"] == "done"
        ev, _ = recv_until(ws, "event.append")
        assert ev["payload"]["type"] == "node_changed" and ev["payload"]["note"] == "node-a status → done"
        assert ev["payload"]["data"]["source"] == "status"
    assert "status: done" in _node_file(project, "node-a").read_text()
    assert _bridge_lines(client) == ["status: node-a status → done"]


# ------------------------------------------------------------------- chat
def test_chat_logs_event_projects_message_and_queues_bridge_line(client: TestClient) -> None:
    with client.websocket_connect("/ws") as ws:
        hello(ws)
        ws.send_json({"type": "chat.message", "payload": {"text": "can we merge?", "agentId": "agent-parser", "nodeId": "node-a"}, "seq": 2})
        ev, seen = recv_until(ws, "event.append")
        assert [m["type"] for m in seen] == ["event.append"]
        p = ev["payload"]
        assert p["type"] == "chat" and p["agent_id"] == "user" and p["note"] == "can we merge?"
        assert p["data"] == {"from": "user", "to": "agent-parser", "thread": "agent-parser",
                             "reply_to": None, "nodeId": "node-a"}
        assert p["notified"] == ["agent-parser"] and ev["seq"] == p["seq"] == 1
        # exactly one chat.message rides along with the event (the Bus projection)
        msg, seen = recv_until(ws, "chat.message")
        assert [m["type"] for m in seen] == ["chat.message"]
        assert msg["payload"] == {
            "id": "chat-1", "thread": "agent-parser", "from": "user", "to": "agent-parser",
            "text": "can we merge?", "ts": p["ts"], "seq": 1, "reply_to": None, "node_id": "node-a",
        }
        # an explicit thread wins, and reply_to is carried through
        ws.send_json({"type": "chat.message", "payload": {"text": "hello root", "thread": "agent-parser", "reply_to": "chat-1"}, "seq": 3})
        ev2, _ = recv_until(ws, "event.append")
        assert ev2["payload"]["data"]["to"] == "root" and ev2["payload"]["node_id"] is None
        assert ev2["payload"]["data"]["thread"] == "agent-parser"
        msg2, _ = recv_until(ws, "chat.message")
        assert msg2["payload"]["thread"] == "agent-parser" and msg2["payload"]["reply_to"] == "chat-1"
        assert msg2["payload"]["id"] == "chat-2"
    assert _bridge_lines(client) == [
        'chat (to: agent-parser, thread agent-parser): "can we merge?"',
        'chat (to: root, thread agent-parser): "hello root"',
    ]
    assert [e["type"] for e in _events(client)] == ["chat", "chat"]


# ------------------------------------------------------------------- plan.paste
def test_paste_mermaid_creates_nodes(client: TestClient, project: Path) -> None:
    text = "```mermaid\nflowchart TD\n    node-x[X box]\n    node-y[Y box]\n    node-x --> node-y\n```"
    with client.websocket_connect("/ws") as ws:
        hello(ws)
        ws.send_json({"type": "plan.paste", "payload": {"text": text}, "seq": 2})
        ack, seen = recv_until(ws, "edit.ack")
        upserts = {m["payload"]["node"]["id"] for m in seen if m["type"] == "plan.node.upsert"}
        assert upserts == {"node-x", "node-y"}
        ev, _ = recv_until(ws, "event.append")
        assert ev["payload"]["type"] == "node_changed" and ev["payload"]["data"]["source"] == "paste"
    store = _state(client).store
    assert store.get_node("node-y").depends_on == ["node-x"] and store.get_node("node-x").title == "X box"
    assert _node_file(project, "node-x").exists()
    assert _bridge_lines(client) == ["plan pasted as diagram hld: node-a, node-b, node-c, node-x, node-y"]


def test_paste_bare_flowchart_and_frontmatter_node(client: TestClient, project: Path) -> None:
    with client.websocket_connect("/ws") as ws:
        hello(ws)
        ws.send_json({"type": "plan.paste", "payload": {"text": "flowchart LR\n  node-p[P] --> node-q[Q]\n"}, "seq": 2})
        recv_until(ws, "edit.ack")
        node_md = "---\nid: node-pasted\ntype: lld\ntitle: Pasted node\ndepends_on:\n  - node-a\ninterfaces:\n  - \"f() -> int\"\n---\n\nBody text here.\n"
        ws.send_json({"type": "plan.paste", "payload": {"text": node_md}, "seq": 3})
        ack, seen = recv_until(ws, "edit.ack")
        assert any(m["type"] == "plan.node.upsert" and m["payload"]["node"]["id"] == "node-pasted" for m in seen)
    store = _state(client).store
    assert store.get_node("node-q").depends_on == ["node-p"]
    n = store.get_node("node-pasted")
    assert n.type == "lld" and n.depends_on == ["node-a"] and n.interfaces == ["f() -> int"] and n.body.strip() == "Body text here."


def test_paste_free_text_logs_plan_pasted(client: TestClient) -> None:
    with client.websocket_connect("/ws") as ws:
        hello(ws)
        ws.send_json({"type": "plan.paste", "payload": {"text": "We need a parser, a store and a UI."}, "seq": 2})
        ev, seen = recv_until(ws, "event.append")
        assert ev["payload"]["type"] == "plan_pasted" and ev["payload"]["data"]["text"] == "We need a parser, a store and a UI."
        assert any(m["type"] == "edit.ack" for m in seen) or recv_until(ws, "edit.ack")
    assert _bridge_lines(client) == ["plan pasted (event 1): structure it with upsert_node/write_diagram"]
    assert len(_state(client).store.nodes) == 3


# ------------------------------------------------------------------- prompt.reply
def test_prompt_reply_round_trip(client: TestClient) -> None:
    log = _state(client).log
    pid = log.create_prompt("agent-parser", "Keep ELK out?", node_id="node-a", kind="confirm", choices=["yes", "no"])
    with client.websocket_connect("/ws") as ws:
        snap, rest = hello(ws)
        assert rest[0]["payload"]["type"] == "needs_input" and rest[0]["payload"]["data"]["prompt_id"] == pid
        ws.send_json({"type": "prompt.reply", "payload": {"prompt_id": pid, "value": "yes"}, "seq": 2})
        ev, seen = recv_until(ws, "event.append")
        assert ev["payload"]["type"] == "reply" and ev["payload"]["data"] == {"prompt_id": pid, "value": "yes"}
        assert ev["payload"]["notified"] == ["agent-parser"] and ev["payload"]["node_id"] == "node-a"
        ws.send_json({"type": "prompt.reply", "payload": {"prompt_id": "zzz", "value": 1}, "seq": 3})
        err, _ = recv_until(ws, "server.error")
        assert err["payload"]["code"] == "unknown_prompt"
    assert log.get_prompt(pid)["answered"] is True and log.get_prompt(pid)["value"] == "yes"
    assert _bridge_lines(client) == [f'needs_input reply (prompt {pid}, agent-parser): "yes"']


# ------------------------------------------------------------------- dispatch.reply
def test_dispatch_reply_approved_and_rejected(client: TestClient) -> None:
    st = _state(client)
    st.log.append(agent_id="root", node_id="node-c", type="dispatch_proposed", note="propose",
                  data={"request_id": "r-1", "node_id": "node-c", "agent_id": "agent-c"})
    st.log.append(agent_id="root", node_id="node-a", type="dispatch_proposed", note="propose",
                  data={"request_id": "r-2", "node_id": "node-a", "agent_id": "agent-a"})
    with client.websocket_connect("/ws") as ws:
        hello(ws)
        ws.send_json({"type": "dispatch.reply", "payload": {"request_id": "r-1", "approved": True}, "seq": 2})
        ev, seen = recv_until(ws, "event.append")
        assert ev["payload"]["type"] == "dispatch_approved"
        assert ev["payload"]["data"] == {"request_id": "r-1", "node_id": "node-c", "agent_id": "agent-c", "note": ""}
        assert ev["payload"]["node_id"] == "node-c" and ev["payload"]["notified"] == ["agent-c"]
        owner_up = next(m for m in seen if m["type"] == "plan.node.upsert")
        assert owner_up["payload"]["node"]["owner"] == "agent-c"
        ws.send_json({"type": "dispatch.reply", "payload": {"request_id": "r-2", "approved": False, "note": "later"}, "seq": 3})
        ev2, _ = recv_until(ws, "event.append")
        assert ev2["payload"]["type"] == "dispatch_rejected" and ev2["payload"]["note"] == "later"
        ws.send_json({"type": "dispatch.reply", "payload": {"request_id": "r-9", "approved": True}, "seq": 4})
        err, _ = recv_until(ws, "server.error")
        assert err["payload"]["code"] == "unknown_request"
    assert st.store.get_node("node-c").owner == "agent-c" and st.store.get_node("node-a").owner is None
    assert _bridge_lines(client) == [
        "dispatch approved: node-c → agent-c (spawn it now with subagent_type=whiteboard-task; job spec: .whiteboard/agents/agent-c/plan.md)",
        'dispatch rejected: node-a → agent-a — "later"',
    ]


# ------------------------------------------------------------------- layout
def test_layout_goes_to_other_clients_only_and_relayout_clears_unpinned(client: TestClient, project: Path) -> None:
    with client.websocket_connect("/ws") as a, client.websocket_connect("/ws") as b:
        hello(a, client_id="a")
        hello(b, client_id="b")
        patch = {"nodes": {"node-a": {"x": 10, "y": 20, "pinned": True}, "node-b": {"x": 30, "y": 40}}, "edges": {"node-a__node-b": {"precise": True}}}
        a.send_json({"type": "canvas.layout", "payload": {"diagram": "hld", "patch": patch}, "seq": 2})
        upd, _ = recv_until(b, "layout.update")
        assert upd["payload"]["diagram"] == "hld" and upd["payload"]["layout"]["nodes"]["node-b"] == {"x": 30, "y": 40}
        sidecar = json.loads((project / ".whiteboard" / "plan" / "hld.layout.json").read_text())
        assert sidecar["nodes"]["node-a"]["pinned"] is True
        # no event, no push for layout
        assert _events(client) == [] and _bridge_lines(client) == []
        b.send_json({"type": "plan.relayout", "payload": {"diagram": "hld"}, "seq": 2})
        re_a, _ = recv_until(a, "layout.update")
        re_b, _ = recv_until(b, "layout.update")
        assert re_a["payload"]["layout"]["nodes"] == {"node-a": {"x": 10, "y": 20, "pinned": True}}
        assert re_a["payload"]["layout"]["edges"] == {} and re_b["payload"]["relayout"] is True
        # a never received its own layout.update
        a.send_json({"type": "chat.message", "payload": {"text": "ping"}, "seq": 3})
        ev, seen = recv_until(a, "event.append")
        assert [m["type"] for m in seen] == ["event.append"]
        # sender of the layout patch (a) only saw the relayout broadcast
        assert re_a["payload"]["relayout"] is True


# ------------------------------------------------------------------- origin
def test_origin_rejected(client: TestClient) -> None:
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect("/ws", headers={"origin": "http://evil.example"}):
            pass
    assert exc.value.code == 1008
    for origin in ("http://localhost:5173", "http://127.0.0.1:43217", "http://localhost"):
        with client.websocket_connect("/ws", headers={"origin": origin}) as ws:
            hello(ws)


# ------------------------------------------------------------------- watcher
@pytest.mark.slow
def test_external_file_edit_reaches_websocket(client: TestClient, project: Path) -> None:
    path = _node_file(project, "node-c")
    time.sleep(0.5)  # FSEvents warm-up
    with client.websocket_connect("/ws") as ws:
        hello(ws)
        path.write_text(path.read_text().replace("title: C thing", "title: C edited"))
        up, seen = recv_until(ws, "plan.node.upsert", timeout=3.0)
        assert up["payload"]["node"]["title"] == "C edited"
        ev, _ = recv_until(ws, "event.append", timeout=3.0)
        assert ev["payload"]["type"] == "node_changed" and ev["payload"]["agent_id"] == "user"
        assert ev["payload"]["data"]["source"] == "file" and ev["payload"]["node_id"] == "node-c"
        assert "renamed to 'C edited'" in ev["payload"]["note"]
    assert _bridge_lines(client) == ["external edit: node-c: renamed to 'C edited' [.whiteboard/plan/nodes/node-c.md]"]
    assert _state(client).store.get_node("node-c").title == "C edited"


@pytest.mark.slow
def test_external_events_jsonl_append_is_tailed(client: TestClient, project: Path) -> None:
    with client.websocket_connect("/ws") as ws:
        hello(ws)
        rec = {"seq": 1, "ts": "2026-09-20T00:00:00.000Z", "agent_id": "agent-x", "node_id": None, "type": "info", "note": "from another process", "notified": [], "data": {}}
        with (project / ".whiteboard" / "events.jsonl").open("a") as fh:
            fh.write(json.dumps(rec) + "\n")
        ev, _ = recv_until(ws, "event.append", timeout=3.0)
        assert ev["seq"] == 1 and ev["payload"]["note"] == "from another process"
    assert _state(client).log.latest_seq == 1


def test_dispatch_reply_is_idempotent(client: TestClient) -> None:
    """A dispatch is decided once: a duplicate reply must not append a second
    decision, or events.jsonl stops being a truthful audit trail."""
    st = _state(client)
    st.log.append(agent_id="root", node_id="node-c", type="dispatch_proposed", note="propose",
                  data={"request_id": "dup-1", "node_id": "node-c", "agent_id": "agent-c"})
    with client.websocket_connect("/ws") as ws:
        hello(ws)
        ws.send_json({"type": "dispatch.reply", "payload": {"request_id": "dup-1", "approved": True}, "seq": 2})
        ev, _ = recv_until(ws, "event.append")
        assert ev["payload"]["type"] == "dispatch_approved"
        ws.send_json({"type": "dispatch.reply", "payload": {"request_id": "dup-1", "approved": True}, "seq": 3})
        err, _ = recv_until(ws, "server.error")
        assert err["payload"]["code"] == "already_resolved"
    decisions = [e for e in st.log.read_since(0, limit=500)
                 if e.data.get("request_id") == "dup-1" and e.type.startswith("dispatch_a")]
    assert len(decisions) == 1


# ------------------------------------------------------------------- diagram.reply
PROPOSED = "flowchart TD\n    node-a[A thing]\n    node-b[B thing]\n    node-c[C thing]\n    node-a --> node-b\n"
EDITED = PROPOSED.replace("    node-a --> node-b\n", "    node-d[D thing]\n    node-a --> node-b\n")


def _propose(client: TestClient, *, request_id: str = "d-1", name: str = "hld", mermaid: str = PROPOSED) -> str:
    """Append the `diagram_proposed` event `propose_diagram` would, and rebuild pending."""
    st = _state(client)
    st.log.append(
        agent_id="root", node_id=None, type="diagram_proposed", note=f"propose diagram {name}",
        data={"request_id": request_id, "name": name, "mermaid": mermaid, "rationale": "because", "diff": {}},
    )
    st.ctx.restore_pending_diagrams()
    return request_id


def test_diagram_reply_approves_the_owners_edit_and_is_idempotent(client: TestClient, project: Path) -> None:
    rid = _propose(client)
    with client.websocket_connect("/ws") as ws:
        hello(ws)
        ws.send_json({"type": "diagram.reply", "payload": {"request_id": rid, "approved": True, "note": "ship it", "mermaid": EDITED}, "seq": 2})
        ack, seen = recv_until(ws, "edit.ack")
        assert {m["payload"]["node"]["id"] for m in seen if m["type"] == "plan.node.upsert"} >= {"node-d"}
        assert ack["payload"]["forSeq"] == 2
        ev, _ = recv_until(ws, "event.append")
        assert ev["payload"]["type"] == "diagram_approved" and ev["payload"]["agent_id"] == "user"
        assert ev["payload"]["data"] == {
            "request_id": rid, "name": "hld", "note": "ship it",
            "edited": True, "created": ["node-d"], "edges": 1,
        }
        # decided once: a re-delivered reply must not append a second decision
        ws.send_json({"type": "diagram.reply", "payload": {"request_id": rid, "approved": False}, "seq": 3})
        err, _ = recv_until(ws, "server.error")
        assert err["payload"]["code"] == "already_resolved"
    st = _state(client)
    assert st.store.pending_diagrams == {}
    assert st.store.get_node("node-d") is not None
    assert "node-d[D thing]" in (project / ".whiteboard" / "plan" / "hld.md").read_text()
    assert _bridge_lines(client) == [
        f'diagram approved: hld (req {rid}; 4 nodes, 1 edges; created node-d) — "ship it"'
    ]
    assert [e["type"] for e in _events(client)] == ["diagram_proposed", "diagram_approved"]


def test_diagram_reply_rejected_writes_nothing(client: TestClient, project: Path) -> None:
    before = (project / ".whiteboard" / "plan" / "hld.md").read_text()
    rid = _propose(client, request_id="d-2", mermaid=EDITED)
    with client.websocket_connect("/ws") as ws:
        hello(ws)
        ws.send_json({"type": "diagram.reply", "payload": {"request_id": rid, "approved": False, "note": "not yet"}, "seq": 2})
        ack, seen = recv_until(ws, "edit.ack")
        assert not any(m["type"].startswith("plan.") for m in seen)
        ev, _ = recv_until(ws, "event.append")
        assert ev["payload"]["type"] == "diagram_rejected"
        assert ev["payload"]["data"] == {"request_id": rid, "name": "hld", "note": "not yet"}
        ws.send_json({"type": "diagram.reply", "payload": {"request_id": "nope", "approved": True}, "seq": 3})
        err, _ = recv_until(ws, "server.error")
        assert err["payload"]["code"] == "unknown_request"
    st = _state(client)
    assert st.store.pending_diagrams == {} and st.store.get_node("node-d") is None
    assert (project / ".whiteboard" / "plan" / "hld.md").read_text() == before
    assert _bridge_lines(client) == [f'diagram rejected: hld (req {rid}) — "not yet"']


def test_bad_mermaid_keeps_the_proposal_pending(client: TestClient) -> None:
    rid = _propose(client, request_id="d-3")
    with client.websocket_connect("/ws") as ws:
        hello(ws)
        ws.send_json({"type": "diagram.reply", "payload": {"request_id": rid, "approved": True, "mermaid": "flowchart TD\n    node-a -->\n"}, "seq": 2})
        err, seen = recv_until(ws, "server.error")
        assert err["payload"]["code"] == "bad_diagram" and not any(m["type"].startswith("plan.") for m in seen)
        assert rid in _state(client).store.pending_diagrams  # still open: fix it and re-reply
        ws.send_json({"type": "diagram.reply", "payload": {"request_id": rid, "approved": True}, "seq": 3})
        recv_until(ws, "edit.ack")
        ev, _ = recv_until(ws, "event.append")
        assert ev["payload"]["type"] == "diagram_approved" and ev["payload"]["data"]["edited"] is False
    assert _state(client).store.pending_diagrams == {}
    assert _bridge_lines(client) == [f"diagram approved: hld (req {rid}; 3 nodes, 1 edges)"]


def test_hello_resends_pending_diagram_requests(client: TestClient) -> None:
    rid = _propose(client, request_id="d-4", mermaid=EDITED)
    with client.websocket_connect("/ws") as ws:
        _snap, rest = hello(ws, last_seq=99)
        req = next(m for m in rest if m["type"] == "diagram.request")
        p = req["payload"]
        assert p["request_id"] == rid and p["name"] == "hld" and p["mermaid"] == EDITED
        assert p["rationale"] == "because"
        # the diff is recomputed against the board as it is now
        assert [n["id"] for n in p["nodes_added"]] == ["node-d"] and p["nodes_removed"] == []
        assert [m["type"] for m in rest] == ["diagram.request", "bridge.status"]


# ------------------------------------------------------------------- chat backfill
def test_hello_replays_chat_threads(client: TestClient) -> None:
    log = _state(client).log
    log.append(agent_id="user", node_id=None, type="chat", note="hi root",
               data={"from": "user", "to": "root", "thread": "root"})
    log.append(agent_id="root", node_id=None, type="chat", note="hi back",
               data={"from": "root", "to": "user", "thread": "root"})
    log.append(agent_id="user", node_id="node-a", type="chat", note="status?",
               data={"from": "user", "to": "agent-parser", "thread": "agent-parser"})
    log.append(agent_id="root", node_id=None, type="info", note="not a chat")
    with client.websocket_connect("/ws") as ws:
        _snap, rest = hello(ws, last_seq=99)  # nothing replayed as event.append
        messages = [m["payload"] for m in rest if m["type"] == "chat.message"]
        assert [m["type"] for m in rest] == ["chat.message"] * 3 + ["bridge.status"]
        assert [m["seq"] for m in messages] == [1, 2, 3]  # one per chat event, ascending
        assert [m["thread"] for m in messages] == ["root", "root", "agent-parser"]
        assert messages[1]["from"] == "root" and messages[1]["to"] == "user"
        assert messages[2]["id"] == "chat-3" and messages[2]["node_id"] == "node-a"


# ------------------------------------------------------------------- agent edit-in-run
def _agent(client: TestClient, agent_id: str = "agent-a", node_id: str = "node-a"):
    return _state(client).store.upsert_agent(agent_id, node_id)


def test_agent_plan_edit_from_canvas(client: TestClient, project: Path) -> None:
    _agent(client)
    with client.websocket_connect("/ws") as ws:
        hello(ws)
        ws.send_json({"type": "agent.plan.edit", "payload": {"agent_id": "agent-zz", "plan_md": "x"}, "seq": 2})
        err, _ = recv_until(ws, "server.error")
        assert err["payload"]["code"] == "unknown_agent"
        ws.send_json({"type": "agent.plan.edit", "payload": {"agent_id": "agent-a", "plan_md": "# Job spec\n\nUse the fixture corpus.\n"}, "seq": 3})
        ack, seen = recv_until(ws, "edit.ack")
        card = next(m for m in seen if m["type"] == "agent.card.upsert")["payload"]["agent"]
        assert "Use the fixture corpus." in card["plan_md"] and ack["payload"]["forSeq"] == 3
        ev, _ = recv_until(ws, "event.append")
        p = ev["payload"]
        assert p["type"] == "agent_plan_edited" and p["notified"] == ["agent-a"] and p["node_id"] == "node-a"
        assert p["data"]["source"] == "canvas" and p["data"]["field"] == "plan_md"
        assert p["data"]["path"] == ".whiteboard/agents/agent-a/plan.md"
        assert "+Use the fixture corpus." in p["data"]["diff"]
        assert p["note"] == "agent plan edited: agent-a — # Job spec"
        # an identical rewrite is acked and nothing else
        ws.send_json({"type": "agent.plan.edit", "payload": {"agent_id": "agent-a", "plan_md": "# Job spec\n\nUse the fixture corpus.\n"}, "seq": 4})
        ack2, seen = recv_until(ws, "edit.ack")
        assert [m["type"] for m in seen] == ["edit.ack"] and ack2["payload"]["forSeq"] == 4
    assert (project / ".whiteboard" / "agents" / "agent-a" / "plan.md").read_text() == "# Job spec\n\nUse the fixture corpus.\n"
    assert _bridge_lines(client) == ["agent plan edited: agent-a — # Job spec"]
    assert [e["type"] for e in _events(client)] == ["agent_plan_edited"]


def test_agent_diagram_edit_rejects_bad_mermaid(client: TestClient) -> None:
    _agent(client)
    with client.websocket_connect("/ws") as ws:
        hello(ws)
        ws.send_json({"type": "agent.diagram.edit", "payload": {"agent_id": "agent-a", "mermaid": "flowchart TD\n    A -->\n"}, "seq": 2})
        rej, seen = recv_until(ws, "edit.reject")
        assert rej["payload"]["revert"] == [] and rej["payload"]["forSeq"] == 2
        assert "invalid mermaid" in rej["payload"]["reason"]
        assert not any(m["type"] == "agent.card.upsert" for m in seen)
        ws.send_json({"type": "agent.diagram.edit", "payload": {"agent_id": "agent-a", "mermaid": "flowchart TD\n    Parser[Parser] --> Ast[Ast]\n"}, "seq": 3})
        ack, seen = recv_until(ws, "edit.ack")
        card = next(m for m in seen if m["type"] == "agent.card.upsert")["payload"]["agent"]
        assert [n["id"] for n in card["diagram"]["nodes"]] == ["Parser", "Ast"]
        ev, _ = recv_until(ws, "event.append")
        assert ev["payload"]["type"] == "agent_plan_edited" and ev["payload"]["data"]["field"] == "diagrams_md"
        assert ev["payload"]["data"]["path"] == ".whiteboard/agents/agent-a/diagrams.md"
    assert _bridge_lines(client) == ["agent diagram edited: agent-a — Parser[Parser] --> Ast[Ast]"]


@pytest.mark.slow
def test_external_agent_plan_edit_reaches_root(client: TestClient, project: Path) -> None:
    _agent(client)
    path = project / ".whiteboard" / "agents" / "agent-a" / "plan.md"
    time.sleep(0.5)  # FSEvents warm-up
    with client.websocket_connect("/ws") as ws:
        hello(ws)
        path.write_text("# Job spec: node-a\n\nAlso vendor the parser.\n")
        up, _ = recv_until(ws, "agent.card.upsert", timeout=3.0)
        assert "Also vendor the parser." in up["payload"]["agent"]["plan_md"]
        ev, _ = recv_until(ws, "event.append", timeout=3.0)
        p = ev["payload"]
        assert p["type"] == "agent_plan_edited" and p["agent_id"] == "user" and p["notified"] == ["agent-a"]
        assert p["data"]["source"] == "file" and p["data"]["field"] == "plan_md"
        assert p["data"]["path"] == ".whiteboard/agents/agent-a/plan.md"
    # pushed verbatim: no "external edit:" prefix, no [path] suffix
    assert _bridge_lines(client) == ["agent plan edited: agent-a — Also vendor the parser."]
