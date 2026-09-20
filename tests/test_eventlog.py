import asyncio
import json
import re
from pathlib import Path

import pytest

from whiteboard.events import EventLog
from whiteboard.plan.model import Event

TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")


@pytest.fixture
def path(tmp_path: Path) -> Path:
    return tmp_path / ".whiteboard" / "events.jsonl"


def _lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_append_assigns_seq_and_ts_and_writes_lines(path: Path) -> None:
    log = EventLog(path)
    log.load()
    e1 = log.append(agent_id="agent-a", node_id="node-a", type="info", note="one")
    e2 = log.append(agent_id="root", node_id=None, type="chat", note="two", data={"to": "agent-a"})
    assert (e1.seq, e2.seq) == (1, 2)
    assert TS_RE.match(e1.ts) and TS_RE.match(e2.ts)
    assert log.latest_seq == 2
    recs = _lines(path)
    assert [r["seq"] for r in recs] == [1, 2]
    assert recs[1]["data"] == {"to": "agent-a"}
    assert recs[0]["notified"] == [] and recs[0]["node_id"] == "node-a"
    assert isinstance(e1, Event)


def test_load_restores_latest_seq_and_ring(path: Path) -> None:
    log = EventLog(path)
    log.load()
    for i in range(5):
        log.append(agent_id="a", node_id=None, type="info", note=str(i))
    other = EventLog(path)
    other.load()
    assert other.latest_seq == 5
    assert [e.note for e in other.read_since(0)] == ["0", "1", "2", "3", "4"]
    nxt = other.append(agent_id="a", node_id=None, type="info", note="5")
    assert nxt.seq == 6


def test_load_skips_bad_lines(path: Path) -> None:
    path.parent.mkdir(parents=True)
    path.write_text(
        '{"seq": 1, "ts": "t", "agent_id": "a", "node_id": null, "type": "info"}\n'
        "this is not json\n"
        '{"seq": "nope"}\n'
        '{"seq": 2, "ts": "t", "agent_id": "a", "node_id": null, "type": "done", "note": "x"}\n'
    )
    log = EventLog(path)
    log.load()
    assert log.latest_seq == 2
    assert [e.seq for e in log.read_since(0)] == [1, 2]


def test_append_without_explicit_load_continues_from_file(path: Path) -> None:
    first = EventLog(path)
    first.append(agent_id="a", node_id=None, type="info")
    first.append(agent_id="a", node_id=None, type="info")
    second = EventLog(path)
    assert second.append(agent_id="b", node_id=None, type="info").seq == 3


def test_read_since_from_ring_with_limit(path: Path) -> None:
    log = EventLog(path)
    log.load()
    for i in range(10):
        log.append(agent_id="a", node_id=None, type="info", note=str(i))
    assert [e.seq for e in log.read_since(7)] == [8, 9, 10]
    assert [e.seq for e in log.read_since(0, limit=3)] == [1, 2, 3]
    assert log.read_since(10) == []
    assert log.read_since(99) == []


def test_read_since_falls_back_to_file_when_older_than_ring(path: Path) -> None:
    log = EventLog(path, ring=3)
    log.load()
    for i in range(10):
        log.append(agent_id="a", node_id=None, type="info", note=str(i))
    assert len(log) == 3
    # Ring holds 8..10 only; asking for everything after 2 must scan the file.
    assert [e.seq for e in log.read_since(2)] == list(range(3, 11))
    # Still served from the ring when it reaches back far enough.
    assert [e.seq for e in log.read_since(8)] == [9, 10]


def test_tail_picks_up_records_from_another_writer(path: Path) -> None:
    mine = EventLog(path)
    mine.load()
    mine.append(agent_id="a", node_id=None, type="info", note="mine")

    other = EventLog(path)
    other.load()
    assert other.latest_seq == 1
    other.append(agent_id="b", node_id=None, type="done", note="theirs")
    other.append(agent_id="b", node_id=None, type="done", note="theirs-2")

    new = mine.tail()
    assert [(e.seq, e.note) for e in new] == [(2, "theirs"), (3, "theirs-2")]
    assert mine.latest_seq == 3
    assert mine.tail() == []
    # Our next append continues after what the other process wrote.
    assert mine.append(agent_id="a", node_id=None, type="info").seq == 4
    assert [e.seq for e in mine.read_since(0)] == [1, 2, 3, 4]


def test_tail_dedupes_after_truncation_redelivery(path: Path) -> None:
    log = EventLog(path)
    log.load()
    log.append(agent_id="a", node_id=None, type="info", note="one")
    other = EventLog(path)
    other.load()
    other.append(agent_id="b", node_id=None, type="info", note="two")
    assert [e.seq for e in log.tail()] == [2]
    # Rewrite the file with the same content: the tailer restarts from byte 0
    # and re-delivers both records; both must be dropped by seq.
    content = path.read_bytes()
    path.write_bytes(b"")
    path.write_bytes(content)
    assert log.tail() == []
    assert log.latest_seq == 2
    assert [e.seq for e in log.read_since(0)] == [1, 2]


def test_tail_does_not_redeliver_own_appends(path: Path) -> None:
    log = EventLog(path)
    log.load()
    log.append(agent_id="a", node_id=None, type="info")
    log.append(agent_id="a", node_id=None, type="info")
    assert log.tail() == []
    assert len(log) == 2


def test_subscribers_notified_outside_running_loop(path: Path) -> None:
    log = EventLog(path)
    log.load()
    q = log.subscribe()
    ev = log.append(agent_id="a", node_id=None, type="info", note="hi")
    assert q.get_nowait() is ev
    log.unsubscribe(q)
    log.append(agent_id="a", node_id=None, type="info", note="after")
    assert q.empty()
    log.unsubscribe(q)  # idempotent


async def test_subscriber_wakes_awaiting_getter(path: Path) -> None:
    log = EventLog(path)
    log.load()
    q = log.subscribe()

    async def waiter() -> Event:
        return await asyncio.wait_for(q.get(), timeout=2)

    task = asyncio.create_task(waiter())
    await asyncio.sleep(0)
    ev = log.append(agent_id="a", node_id="node-x", type="done")
    got = await task
    assert got.seq == ev.seq and got.node_id == "node-x"


def test_subscribers_notified_on_tail(path: Path) -> None:
    log = EventLog(path)
    log.load()
    q = log.subscribe()
    other = EventLog(path)
    other.load()
    other.append(agent_id="b", node_id=None, type="info", note="remote")
    assert q.empty()
    log.tail()
    assert q.get_nowait().note == "remote"


def test_prompts_create_and_answer(path: Path) -> None:
    log = EventLog(path)
    log.load()
    pid = log.create_prompt("agent-a", "Keep ELK out?", node_id="node-a", kind="choice", choices=["yes", "no"])
    assert re.match(r"^[0-9a-f]{8}$", pid)
    assert log.pending_prompts[pid] == {
        "agent_id": "agent-a",
        "node_id": "node-a",
        "question": "Keep ELK out?",
        "kind": "choice",
        "choices": ["yes", "no"],
        "answered": False,
        "value": None,
    }
    ev = log.read_since(0)[0]
    assert ev.type == "needs_input" and ev.data["prompt_id"] == pid and ev.note == "Keep ELK out?"

    reply = log.answer_prompt(pid, "yes")
    assert reply.type == "reply" and reply.data == {"prompt_id": pid, "value": "yes"}
    assert reply.agent_id == "user" and reply.node_id == "node-a" and reply.notified == ["agent-a"]
    assert log.pending_prompts[pid]["answered"] is True
    assert log.pending_prompts[pid]["value"] == "yes"
    assert log.get_prompt(pid)["answered"] is True
    assert log.get_prompt("nope") is None


def test_prompts_rebuilt_from_log_on_load(path: Path) -> None:
    log = EventLog(path)
    log.load()
    p1 = log.create_prompt("agent-a", "first?")
    p2 = log.create_prompt("agent-b", "second?", kind="confirm")
    log.answer_prompt(p1, True)

    fresh = EventLog(path)
    fresh.load()
    assert set(fresh.pending_prompts) == {p1, p2}
    assert fresh.pending_prompts[p1]["answered"] is True and fresh.pending_prompts[p1]["value"] is True
    assert fresh.pending_prompts[p2]["answered"] is False and fresh.pending_prompts[p2]["kind"] == "confirm"
    # Prompts created by another process show up after tail().
    p3 = log.create_prompt("agent-c", "third?")
    fresh.tail()
    assert fresh.pending_prompts[p3]["question"] == "third?"


def test_answer_unknown_prompt_raises(path: Path) -> None:
    log = EventLog(path)
    log.load()
    with pytest.raises(KeyError):
        log.answer_prompt("deadbeef", "x")
    with pytest.raises(ValueError):
        log.create_prompt("agent-a", "bad kind", kind="menu")


def test_unresolved(path: Path) -> None:
    log = EventLog(path)
    log.load()

    def propose(rid: str, name: str) -> None:
        log.append(agent_id="root", node_id=None, type="diagram_proposed", note=name,
                   data={"request_id": rid, "name": name})

    propose("aaaa1111", "hld")
    propose("bbbb2222", "lld")
    propose("cccc3333", "er")
    log.append(agent_id="user", node_id=None, type="diagram_approved", note="",
               data={"request_id": "aaaa1111"})
    log.append(agent_id="user", node_id=None, type="diagram_rejected", note="",
               data={"request_id": "cccc3333"})
    # unrelated request/decision pairs and events without a request_id are ignored
    log.append(agent_id="root", node_id=None, type="dispatch_proposed", note="",
               data={"request_id": "dddd4444"})
    log.append(agent_id="agent-a", node_id=None, type="info", note="no request id")

    settled = ("diagram_approved", "diagram_rejected")
    assert [e.data["request_id"] for e in log.unresolved("diagram_proposed", settled)] == ["bbbb2222"]
    assert [e.data["request_id"] for e in log.unresolved("dispatch_proposed",
                                                         ("dispatch_approved", "dispatch_rejected"))] == ["dddd4444"]
    # a bare string settled type is accepted too
    assert [e.data["request_id"] for e in log.unresolved("diagram_proposed", "diagram_approved")] == [
        "bbbb2222", "cccc3333"
    ]
    # the latest proposal of a re-proposed request wins, and events stay in seq order
    propose("bbbb2222", "lld v2")
    propose("eeee5555", "hld v2")
    still_open = log.unresolved("diagram_proposed", settled)
    assert [e.data["request_id"] for e in still_open] == ["bbbb2222", "eeee5555"]
    assert still_open[0].note == "lld v2" and [e.seq for e in still_open] == sorted(e.seq for e in still_open)

    # it is derived from the file, so a fresh log (a restart) sees the same thing
    fresh = EventLog(path)
    fresh.load()
    assert [e.data["request_id"] for e in fresh.unresolved("diagram_proposed", settled)] == [
        "bbbb2222", "eeee5555"
    ]
    assert log.unresolved("plan_pasted", settled) == []


def test_ring_bounded(path: Path) -> None:
    log = EventLog(path, ring=5)
    log.load()
    for _ in range(12):
        log.append(agent_id="a", node_id=None, type="info")
    assert len(log) == 5
    assert [e.seq for e in log.read_since(7)] == [8, 9, 10, 11, 12]
    assert log.latest_seq == 12
