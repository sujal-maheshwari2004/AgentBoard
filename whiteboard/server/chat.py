"""Chat threading: projecting ``chat`` events into ``chat.message`` frames.

One source of truth: every chat line — typed by the human on the canvas, sent
by the root session with ``chat_reply`` or appended by a subagent — is a ``chat``
event in ``events.jsonl``. This module turns such an event into the
``chat.message`` payload the canvas renders (CONTRACTS §6) and computes the
thread it belongs to. The ``Bus`` projects live events; :func:`chat_backfill`
replays the last :data:`CHAT_BACKFILL_PER_THREAD` messages per thread on
``client.hello``.

Pure and dependency-free (it needs only an :class:`~whiteboard.plan.model.Event`
and an :class:`~whiteboard.events.log.EventLog`), so it imports without the app.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "CHAT_BACKFILL_PER_THREAD",
    "chat_backfill",
    "chat_message_from_event",
    "thread_for",
]

#: How many chat messages per thread ``client.hello`` replays.
CHAT_BACKFILL_PER_THREAD = 50

#: Limit for the whole-log scan in :func:`chat_backfill`.
_ALL = 1_000_000

#: Senders whose own thread is the root thread.
_ROOT_SENDERS: tuple[str, ...] = ("root", "server")


def thread_for(sender: str, to: str | None, explicit: str | None) -> str:
    """The thread a chat line belongs to.

    An explicit thread always wins. Otherwise a message from the human belongs
    to the thread of whoever it is addressed to (``root`` when nobody), and a
    message from root or an agent belongs to that participant's own thread.
    """
    if explicit and str(explicit).strip():
        return str(explicit).strip()
    sender = (sender or "").strip()
    to = (to or "").strip()
    if sender in _ROOT_SENDERS:
        return "root"
    if sender and sender != "user":
        return sender
    return to if to and to != "user" else "root"


def chat_message_from_event(ev: Any) -> dict | None:
    """The ``chat.message`` payload for a ``chat`` event, or None for anything else."""
    if getattr(ev, "type", None) != "chat":
        return None
    data = ev.data if isinstance(ev.data, dict) else {}
    sender = str(data.get("from") or ev.agent_id or "")
    to = data.get("to")
    reply_to = data.get("reply_to")
    return {
        "id": f"chat-{ev.seq}",
        "thread": thread_for(sender, str(to) if to else None, data.get("thread")),
        "from": sender,
        "to": str(to) if to else None,
        "text": ev.note,
        "ts": ev.ts,
        "seq": ev.seq,
        "reply_to": str(reply_to) if reply_to else None,
        "node_id": ev.node_id,
    }


def chat_backfill(log: Any, *, per_thread: int = CHAT_BACKFILL_PER_THREAD) -> list[dict]:
    """The last ``per_thread`` chat messages of every thread, oldest first."""
    per_thread = max(0, int(per_thread))
    threads: dict[str, list[dict]] = {}
    for ev in log.read_since(0, limit=_ALL):
        msg = chat_message_from_event(ev)
        if msg is None:
            continue
        threads.setdefault(msg["thread"], []).append(msg)
    out: list[dict] = []
    for messages in threads.values():
        out.extend(messages[-per_thread:] if per_thread else [])
    out.sort(key=lambda m: m["seq"])
    return out
