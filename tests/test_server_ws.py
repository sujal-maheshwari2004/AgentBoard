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
def test_chat_logs_event_and_queues_bridge_line(client: TestClient) -> None:
    with client.websocket_connect("/ws") as ws:
        hello(ws)
        ws.send_json({"type": "chat.message", "payload": {"text": "can we merge?", "agentId": "agent-parser", "nodeId": "node-a"}, "seq": 2})
        ev, seen = recv_until(ws, "event.append")
        assert [m["type"] for m in seen] == ["event.append"]
        p = ev["payload"]
        assert p["type"] == "chat" and p["agent_id"] == "user" and p["note"] == "can we merge?"
        assert p["data"] == {"to": "agent-parser", "nodeId": "node-a"} and p["notified"] == ["agent-parser"]
        assert ev["seq"] == p["seq"] == 1
        ws.send_json({"type": "chat.message", "payload": {"text": "hello root"}, "seq": 3})
        ev2, _ = recv_until(ws, "event.append")
        assert ev2["payload"]["data"]["to"] == "root" and ev2["payload"]["node_id"] is None
    assert _bridge_lines(client) == ['chat (to: agent-parser): "can we merge?"', 'chat (to: root): "hello root"']
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
