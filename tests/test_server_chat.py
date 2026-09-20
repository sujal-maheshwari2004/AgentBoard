"""Chat threading and the ``chat.message`` projection (CONTRACTS §6/§7)."""

from __future__ import annotations

from pathlib import Path

import pytest

from whiteboard.events.log import EventLog
from whiteboard.plan.model import Event
from whiteboard.server.chat import (
    CHAT_BACKFILL_PER_THREAD,
    chat_backfill,
    chat_message_from_event,
    thread_for,
)


@pytest.fixture
def log(tmp_path: Path) -> EventLog:
    out = EventLog(tmp_path / ".whiteboard" / "events.jsonl")
    out.load()
    return out


def _chat(log: EventLog, sender: str, text: str, **data) -> Event:
    return log.append(agent_id=sender, node_id=data.pop("node_id", None), type="chat", note=text,
                      data={"from": sender, **data})


def test_thread_for() -> None:
    assert thread_for("user", "agent-x", None) == "agent-x"
    assert thread_for("user", None, None) == "root"
    assert thread_for("user", "root", None) == "root"
    assert thread_for("root", "agent-x", None) == "root"  # root always talks on its own thread
    assert thread_for("server", None, None) == "root"
    assert thread_for("agent-x", "user", None) == "agent-x"
    assert thread_for("agent-x", None, None) == "agent-x"
    # an explicit thread always wins, and is trimmed
    assert thread_for("agent-x", "user", "root") == "root"
    assert thread_for("user", "agent-x", " agent-y ") == "agent-y"
    assert thread_for("user", None, "  ") == "root"  # blank is no thread at all


def test_chat_message_from_event(log: EventLog) -> None:
    ev = _chat(log, "agent-x", "on it", to="user", reply_to="chat-3", node_id=None)
    assert chat_message_from_event(ev) == {
        "id": f"chat-{ev.seq}", "thread": "agent-x", "from": "agent-x", "to": "user",
        "text": "on it", "ts": ev.ts, "seq": ev.seq, "reply_to": "chat-3", "node_id": None,
    }
    # the event's data wins over agent_id, and an explicit thread is kept
    ev = log.append(agent_id="user", node_id="node-a", type="chat", note="ping",
                    data={"from": "user", "to": "agent-x", "thread": "agent-x"})
    msg = chat_message_from_event(ev)
    assert msg["from"] == "user" and msg["thread"] == "agent-x" and msg["node_id"] == "node-a"
    assert msg["reply_to"] is None
    # anything that is not a chat event is not a message
    assert chat_message_from_event(log.append(agent_id="root", node_id=None, type="info", note="x")) is None


def test_chat_backfill_groups_by_thread_and_caps(log: EventLog) -> None:
    assert CHAT_BACKFILL_PER_THREAD == 50
    assert chat_backfill(log) == []
    for i in range(4):
        _chat(log, "user", f"u{i}", to="root")
        log.append(agent_id="root", node_id=None, type="info", note="noise")  # not chat
        _chat(log, "agent-x", f"x{i}", to="user")
    _chat(log, "user", "hello agent-y", to="agent-y")

    every = chat_backfill(log)
    assert [m["text"] for m in every] == [
        "u0", "x0", "u1", "x1", "u2", "x2", "u3", "x3", "hello agent-y"
    ]
    assert [m["seq"] for m in every] == sorted(m["seq"] for m in every)  # ascending overall

    capped = chat_backfill(log, per_thread=2)
    by_thread: dict[str, list[str]] = {}
    for m in capped:
        by_thread.setdefault(m["thread"], []).append(m["text"])
    assert by_thread == {"root": ["u2", "u3"], "agent-x": ["x2", "x3"], "agent-y": ["hello agent-y"]}
    assert [m["seq"] for m in capped] == sorted(m["seq"] for m in capped)
    assert chat_backfill(log, per_thread=0) == []
