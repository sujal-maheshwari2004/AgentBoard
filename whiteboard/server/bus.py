"""The ``Bus`` (CONTRACTS §7) and the per-server context shared by the websocket
handlers, the HTTP routes and the MCP tools.

``ServerContext`` owns the DONE building blocks (``PlanStore``, ``EventLog``,
``Hub``, ``ClaudeBridge``) and is reachable as ``app.state.ctx``; the
individual objects are also mirrored onto ``app.state`` (``store``, ``log``,
``hub``, ``bridge``, ``bus``).
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from whiteboard.config import project_name
from whiteboard.events.log import EventLog, now_iso
from whiteboard.files.atomic import SelfWriteRegistry
from whiteboard.plan.model import Event
from whiteboard.plan.store import PlanStore
from whiteboard.server.chat import chat_message_from_event
from whiteboard.server.claude_bridge import ClaudeBridge
from whiteboard.server.hub import Connection, Hub, envelope

__all__ = ["Bus", "ServerContext", "PendingEdit"]

log = logging.getLogger(__name__)


class Bus:
    """Fan-out to websocket clients plus the debounced push to the root session.

    * ``publish(type, payload, seq=None)``: broadcast one envelope to every client.
    * ``publish_messages(messages)``: broadcast ``PlanStore`` messages
      (``store.last_messages`` / ``apply_file_change`` results); an
      ``event.external`` entry is appended to the event log (as ``risky_edit``
      or ``node_changed`` by ``user``) and pushed to root instead of being sent.
    * ``push_root(lines)``: queue bullet lines for the next debounced inbox message.
    * ``bridge_status()``: ``{ok, failures, last_error, last_pushed_seq, socket, ...}``.

    Events appended through ``EventLog.append`` (by anyone) are broadcast as
    ``event.append`` by the server's event pump, so callers do not publish them.

    Every ``chat`` event that survives the ``event.append`` de-duplication is
    also projected into one ``chat.message`` broadcast (CONTRACTS §7), so the
    canvas never builds threads out of raw events and no producer (``on_chat``,
    ``chat_reply``, ``append_event``, the event pump) can double-send one.
    """

    def __init__(self, hub: Hub, bridge: ClaudeBridge, event_log: EventLog) -> None:
        self.hub = hub
        self.bridge = bridge
        self.log = event_log
        # Highest event seq already broadcast: the MCP layer publishes the events
        # it appends itself, and the event pump sees them too; whichever comes
        # first wins, the other is dropped here (seqs are monotonic per log).
        self.last_event_seq = 0

    def publish(self, type: str, payload: Any, *, seq: int | None = None, exclude: Connection | None = None) -> int:
        if type == "event.append":
            ev_seq = seq if isinstance(seq, int) else (payload.get("seq") if isinstance(payload, dict) else None)
            if isinstance(ev_seq, int):
                if ev_seq <= self.last_event_seq:
                    return 0
                self.last_event_seq = ev_seq
                seq = ev_seq
            sent = self.hub.broadcast(envelope(type, payload, seq=seq), exclude=exclude)
            self._project_chat(payload)
            return sent
        return self.hub.broadcast(envelope(type, payload, seq=seq), exclude=exclude)

    def _project_chat(self, payload: Any) -> None:
        """Broadcast the ``chat.message`` view of a ``chat`` event (CONTRACTS §6)."""
        if not isinstance(payload, dict) or payload.get("type") != "chat":
            return
        try:
            message = chat_message_from_event(Event(**payload))
        except Exception:  # pragma: no cover - a malformed event must not break the broadcast
            log.exception("could not project chat event %r", payload.get("seq"))
            return
        if message is not None:
            self.hub.broadcast(envelope("chat.message", message))

    def publish_event(self, event: Event) -> int:
        return self.publish("event.append", event.model_dump(), seq=event.seq)

    def publish_messages(self, messages: list[dict] | None, *, exclude: Connection | None = None) -> list[Event]:
        """Broadcast store messages; returns the events logged for ``event.external`` entries."""
        logged: list[Event] = []
        for msg in messages or []:
            mtype = msg.get("type")
            payload = msg.get("payload") or {}
            if mtype == "event.external":
                ev = self.external_to_event(payload)
                if ev is not None:
                    logged.append(ev)
                continue
            if not isinstance(mtype, str):
                continue
            self.publish(mtype, payload, exclude=exclude)
        return logged

    def external_to_event(self, payload: dict) -> Event | None:
        """Log an external (on-disk) edit and push a line for it."""
        summary = str(payload.get("summary") or "").strip()
        risky = bool(payload.get("risky"))
        ids = [i for i in (payload.get("ids") or []) if isinstance(i, str)]
        path = payload.get("path")
        kind = payload.get("kind")
        if not summary:
            return None
        if kind == "agent" and payload.get("field") and ids:
            return self._agent_edit_event(payload, summary, ids[0], path)
        try:
            ev = self.log.append(
                agent_id="user",
                node_id=ids[0] if len(ids) == 1 else None,
                type="risky_edit" if risky else "node_changed",
                note=summary,
                data={"source": "file", "path": path, "kind": kind, "ids": ids, "risky": risky,
                      **({"error": payload["error"]} if payload.get("error") else {})},
            )
        except Exception:
            log.exception("could not log external edit for %s", path)
            return None
        flat = "; ".join(line.strip() for line in summary.splitlines() if line.strip())
        label = "external edit (risky)" if risky else "external edit"
        self.push_root([f"{label}: {flat} [{path}]"])
        return ev

    def _agent_edit_event(self, payload: dict, summary: str, agent_id: str, path: Any) -> Event | None:
        """A hand edit of ``agents/<id>/plan.md`` or ``diagrams.md`` (A.6.2).

        It is a message *to* a running agent rather than a plan change, so it is
        logged as ``agent_plan_edited`` with the agent notified and pushed as the
        store's summary verbatim (no ``external edit:`` prefix): the root session
        relays it with ``SendMessage`` and waits for the ``report_progress``
        acknowledgement.
        """
        try:
            ev = self.log.append(
                agent_id="user",
                node_id=None,
                type="agent_plan_edited",
                note=summary,
                notified=[agent_id],
                data={
                    "source": "file", "path": path, "kind": "agent", "agent_id": agent_id,
                    "field": payload.get("field"), "diff": payload.get("diff"),
                    "ids": list(payload.get("ids") or []),
                },
            )
        except Exception:
            log.exception("could not log agent edit for %s", path)
            return None
        self.push_root(["; ".join(line.strip() for line in summary.splitlines() if line.strip())])
        return ev

    def push_root(self, lines: list[str]) -> None:
        for line in lines or []:
            try:
                self.bridge.queue_line(line)
            except Exception:  # pragma: no cover - the bridge never raises
                log.exception("bridge.queue_line failed")

    def bridge_status(self) -> dict:
        return self.bridge.status()


def _agent_health(card: Any) -> dict:
    """One agent's row in ``/api/health`` (CONTRACTS §10)."""
    return {
        "id": card.id,
        "assigned_node": card.assigned_node,
        "status": card.status,
        "activity": card.activity,
        "progress": card.progress,
        "spawned_at": card.spawned_at,
        "heartbeat_at": card.heartbeat_at,
        "finished_at": card.finished_at,
        "metrics": dict(card.metrics),
    }


@dataclass
class PendingEdit:
    """Who asked for a parked risky edit, so the ack/reject can be routed back."""

    conn_id: int
    for_seq: int | None
    ops: list[dict]
    summary: str
    created_at: str = field(default_factory=now_iso)


class ServerContext:
    """Everything a request/handler needs, built once per ``create_app``."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root).resolve()
        self.project = project_name(self.root)
        self.wb = self.root / ".whiteboard"
        self.self_writes = SelfWriteRegistry()
        self.store = PlanStore(self.root, self_writes=self.self_writes)
        self.log = EventLog(self.wb / "events.jsonl")
        self.hub = Hub()
        self.bridge = ClaudeBridge(self.project, seq_provider=lambda: self.log.latest_seq)
        self.bus = Bus(self.hub, self.bridge, self.log)
        self.pending_edits: dict[str, PendingEdit] = {}
        self.pid = os.getpid()
        self.port: int = int(os.environ.get("WHITEBOARD_PORT") or 0)
        self.started_at = now_iso()
        self._started_mono = time.monotonic()
        self.loaded = False

    def load(self) -> None:
        self.store.load()
        self.log.load()
        self.restore_pending_diagrams()
        self.loaded = True

    def restore_pending_diagrams(self) -> dict[str, dict]:
        """Rebuild ``store.pending_diagrams`` from the event log (A.2.5).

        A proposal is pending when its ``diagram_proposed`` event has no
        ``diagram_approved`` / ``diagram_rejected`` answer, so a restart neither
        loses a proposal nor resurrects a decided one. ``on_hello`` re-sends one
        ``diagram.request`` per entry (with a freshly computed diff).
        """
        self.store.pending_diagrams.clear()
        for ev in self.log.unresolved("diagram_proposed", ("diagram_approved", "diagram_rejected")):
            data = ev.data if isinstance(ev.data, dict) else {}
            rid = data.get("request_id")
            if not isinstance(rid, str) or not rid:
                continue
            self.store.pending_diagrams[rid] = {
                "name": data.get("name") or "hld",
                "mermaid": data.get("mermaid") or "",
                "rationale": data.get("rationale") or "",
                "agent_id": ev.agent_id,
                "created_at": ev.ts,
                "seq": ev.seq,
            }
        return self.store.pending_diagrams

    @property
    def uptime_s(self) -> float:
        return round(time.monotonic() - self._started_mono, 3)

    def health(self) -> dict:
        return {
            "project": str(self.root),
            "project_name": self.project,
            "pid": self.pid,
            "port": self.port,
            "started_at": self.started_at,
            "uptime_s": self.uptime_s,
            "clients": self.hub.clients,
            "nodes": len(self.store.nodes),
            "agents": [_agent_health(c) for c in sorted(self.store.agents.values(), key=lambda c: c.id)],
            "agent_count": len(self.store.agents),
            "pending_diagrams": len(self.store.pending_diagrams),
            "rev": self.store.rev,
            "latest_seq": self.log.latest_seq,
            "pending_edits": len(self.store.pending_edits),
            "pending_prompts": sum(1 for p in self.log.pending_prompts.values() if not p.get("answered")),
            "bridge": self.bridge.status(),
        }
