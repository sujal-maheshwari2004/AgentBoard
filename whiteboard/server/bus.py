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
    """

    def __init__(self, hub: Hub, bridge: ClaudeBridge, event_log: EventLog) -> None:
        self.hub = hub
        self.bridge = bridge
        self.log = event_log

    def publish(self, type: str, payload: Any, *, seq: int | None = None, exclude: Connection | None = None) -> int:
        return self.hub.broadcast(envelope(type, payload, seq=seq), exclude=exclude)

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

    def push_root(self, lines: list[str]) -> None:
        for line in lines or []:
            try:
                self.bridge.queue_line(line)
            except Exception:  # pragma: no cover - the bridge never raises
                log.exception("bridge.queue_line failed")

    def bridge_status(self) -> dict:
        return self.bridge.status()


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
        self.loaded = True

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
            "agents": len(self.store.agents),
            "rev": self.store.rev,
            "latest_seq": self.log.latest_seq,
            "pending_edits": len(self.store.pending_edits),
            "pending_prompts": sum(1 for p in self.log.pending_prompts.values() if not p.get("answered")),
            "bridge": self.bridge.status(),
        }
