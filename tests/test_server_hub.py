"""Hub (queues, envelopes, eviction) and the client-message protocol models."""

from __future__ import annotations

import asyncio

import pytest
from starlette.websockets import WebSocketState

from whiteboard.server import protocol as P
from whiteboard.server.hub import NON_EVENT_SEQ_BASE, Connection, Hub, envelope


class FakeWS:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.application_state = WebSocketState.CONNECTED
        self.closed: list[tuple[int, str]] = []

    async def send_json(self, data: dict) -> None:
        self.sent.append(data)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed.append((code, reason))
        self.application_state = WebSocketState.DISCONNECTED


async def _settle() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


# ------------------------------------------------------------------- envelope
def test_envelope_shape() -> None:
    env = envelope("plan.snapshot", {"a": 1}, reply_to=7)
    assert set(env) == {"type", "payload", "seq", "ts", "replyTo"}
    assert env["seq"] is None and env["replyTo"] == 7 and env["ts"].endswith("Z")


# ------------------------------------------------------------------- hub
async def test_writer_is_sole_sender_and_stamps_non_event_seq() -> None:
    hub = Hub()
    ws = FakeWS()
    conn = hub.register(ws)  # type: ignore[arg-type]
    assert hub.clients == 1
    hub.send(conn, envelope("bridge.status", {"ok": False}))
    hub.send(conn, envelope("event.append", {"seq": 3}, seq=3))
    hub.send(conn, {"type": "edit.ack", "payload": {"forSeq": 1, "rev": 2}, "replyTo": 1})
    await _settle()
    seqs = [m["seq"] for m in ws.sent]
    assert seqs == [NON_EVENT_SEQ_BASE, 3, NON_EVENT_SEQ_BASE + 1]
    assert ws.sent[2]["replyTo"] == 1 and ws.sent[2]["ts"]
    await hub.unregister(conn)
    assert hub.clients == 0 and conn.writer.done()


async def test_broadcast_excludes_and_counts() -> None:
    hub = Hub()
    a, b = FakeWS(), FakeWS()
    ca, cb = hub.register(a), hub.register(b)  # type: ignore[arg-type]
    n = hub.broadcast(envelope("layout.update", {"diagram": "hld"}), exclude=ca)
    assert n == 1
    await _settle()
    assert a.sent == [] and b.sent[0]["type"] == "layout.update"
    # exclude by raw websocket object works too
    assert hub.broadcast(envelope("x", {}), exclude=b) == 1
    await hub.unregister(ca)
    await hub.unregister(cb)


async def test_queue_full_evicts_client() -> None:
    hub = Hub(maxsize=2)
    ws = FakeWS()
    conn = hub.register(ws)  # type: ignore[arg-type]
    # Block the writer so the queue fills up.
    conn.writer.cancel()
    await _settle()
    assert hub.send(conn, envelope("a", {})) and hub.send(conn, envelope("b", {}))
    assert hub.send(conn, envelope("c", {})) is False
    await _settle()
    assert hub.clients == 0 and hub.evicted == 1
    assert ws.closed and ws.closed[0][0] == 1013


async def test_find_and_connections() -> None:
    hub = Hub()
    ws = FakeWS()
    conn = hub.register(ws)  # type: ignore[arg-type]
    assert hub.find(ws) is conn and hub.connections() == [conn]  # type: ignore[arg-type]
    assert isinstance(conn, Connection) and conn.client_id is None
    await hub.close_all()
    assert hub.clients == 0 and ws.closed


# ------------------------------------------------------------------- protocol
def test_parse_every_client_type() -> None:
    samples = {
        "client.hello": {"clientId": "c1", "lastSeq": 4, "protocol": 1},
        "canvas.edit": {"ops": [{"op": "renamed", "id": "node-a", "label": "A"}]},
        "canvas.layout": {"diagram": "hld", "patch": {"nodes": {"node-a": {"x": 1}}}},
        "node.status": {"id": "node-a", "status": "done"},
        "chat.message": {"text": "hi", "agentId": "agent-x"},
        "plan.paste": {"text": "flowchart TD"},
        "prompt.reply": {"prompt_id": "p1", "value": "yes"},
        "dispatch.reply": {"request_id": "r1", "approved": True},
        "risky_edit.reply": {"request_id": "r1", "approved": False, "note": "no"},
        "plan.relayout": {"diagram": "er"},
    }
    assert set(samples) == set(P.CLIENT_MESSAGE_TYPES)
    for mtype, payload in samples.items():
        msg = P.parse_client_message({"type": mtype, "payload": payload, "seq": 9, "extra": 1})
        assert msg.type == mtype and msg.seq == 9
    hello = P.parse_client_message({"type": "client.hello", "payload": None})
    assert hello.payload.lastSeq == 0 and hello.payload.protocol == 1
    relayout = P.parse_client_message({"type": "plan.relayout"})
    assert relayout.payload.diagram == "hld"


def test_parse_errors() -> None:
    with pytest.raises(P.ProtocolError) as exc:
        P.parse_client_message({"type": "nope", "payload": {}, "seq": 3})
    assert exc.value.code == "unknown_type" and exc.value.for_seq == 3
    with pytest.raises(P.ProtocolError) as exc:
        P.parse_client_message({"payload": {}})
    assert exc.value.code == "bad_envelope"
    with pytest.raises(P.ProtocolError) as exc:
        P.parse_client_message(["not", "a", "dict"])
    assert exc.value.code == "bad_envelope"
    with pytest.raises(P.ProtocolError) as exc:
        P.parse_client_message({"type": "node.status", "payload": {"id": "node-a"}, "seq": 2})
    assert exc.value.code == "bad_payload" and "status" in exc.value.message and exc.value.for_seq == 2
