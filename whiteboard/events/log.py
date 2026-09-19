"""Append-only event log over ``events.jsonl`` with a ring buffer, multi-process
tailing, subscribers and pending prompts (CONTRACTS §5).

Every record is one JSON object per line. ``seq`` is assigned here on append
(``latest_seq + 1``); records appended by *other* processes on the same file
are picked up by :meth:`EventLog.tail` and de-duplicated by ``seq``.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from whiteboard.files.jsonl import JsonlTailer, append_jsonl
from whiteboard.plan.model import Event

__all__ = ["EventLog", "now_iso"]

log = logging.getLogger(__name__)

PROMPT_KINDS: tuple[str, ...] = ("text", "choice", "confirm")


def now_iso() -> str:
    """ISO-8601 UTC with millisecond precision and a ``Z`` suffix."""
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


class EventLog:
    """The project's ``events.jsonl``.

    ``load()`` reads the whole file once; afterwards ``append`` writes through
    ``append_jsonl`` and ``tail`` picks up what other processes appended. Both
    feed the ring (the last ``ring`` events), the subscriber queues and the
    ``pending_prompts`` index (``needs_input`` events answered by ``reply``).
    """

    def __init__(self, path: Path, ring: int = 2000) -> None:
        self.path = Path(path)
        self.ring_size = max(1, int(ring))
        self.latest_seq = 0
        self.pending_prompts: dict[str, dict[str, Any]] = {}
        self._ring: deque[Event] = deque(maxlen=self.ring_size)
        self._subscribers: list[asyncio.Queue[Event]] = []
        self._tailer: JsonlTailer | None = None
        self._loaded = False

    # -- loading -------------------------------------------------------------
    def load(self) -> None:
        """Read the whole file (bad lines skipped), set ``latest_seq``, fill the
        ring, rebuild ``pending_prompts`` and position the tailer at EOF."""
        self._ring.clear()
        self.latest_seq = 0
        self.pending_prompts = {}
        tailer = JsonlTailer(self.path)
        for rec in tailer.read_all():
            self._ingest(rec, notify=False)
        self._tailer = tailer
        self._loaded = True

    def _ensure_loaded(self) -> None:
        if not self._loaded:
            self.load()

    def _ingest(self, rec: dict, *, notify: bool) -> Event | None:
        try:
            ev = Event(**rec)
        except (ValidationError, TypeError) as exc:
            log.warning("skipping invalid event record in %s: %s", self.path, exc)
            return None
        if ev.seq > self.latest_seq:
            self.latest_seq = ev.seq
        self._ring.append(ev)
        self._track_prompt(ev)
        if notify:
            self._notify(ev)
        return ev

    # -- writing -------------------------------------------------------------
    def append(
        self,
        *,
        agent_id: str,
        node_id: str | None,
        type: str,
        note: str = "",
        notified: list[str] | None = None,
        data: dict | None = None,
    ) -> Event:
        """Assign ``seq``/``ts``, write the record, ring it, notify subscribers."""
        self._ensure_loaded()
        # Pick up other writers first so our seq is never stale.
        self._tail_records()
        ev = Event(
            seq=self.latest_seq + 1,
            ts=now_iso(),
            agent_id=agent_id,
            node_id=node_id,
            type=type,
            note=note or "",
            notified=list(notified or []),
            data=dict(data or {}),
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        append_jsonl(self.path, ev.model_dump())
        self.latest_seq = ev.seq
        self._ring.append(ev)
        self._track_prompt(ev)
        self._notify(ev)
        return ev

    # -- reading -------------------------------------------------------------
    def read_since(self, since: int, limit: int = 500) -> list[Event]:
        """Events with ``seq > since`` in seq order, at most ``limit``. Served
        from the ring when it reaches back far enough, else from the file."""
        self._ensure_loaded()
        since = int(since or 0)
        limit = max(0, int(limit))
        if self._ring and (self._ring[0].seq <= since + 1 or since >= self.latest_seq):
            events = [e for e in self._ring if e.seq > since]
        else:
            events = [e for e in self._scan_file() if e.seq > since]
        events.sort(key=lambda e: e.seq)
        return events[:limit]

    def _scan_file(self) -> list[Event]:
        out: list[Event] = []
        seen: set[int] = set()
        for rec in JsonlTailer(self.path).read_all():
            try:
                ev = Event(**rec)
            except (ValidationError, TypeError):
                continue
            if ev.seq in seen:
                continue
            seen.add(ev.seq)
            out.append(ev)
        return out

    def tail(self) -> list[Event]:
        """New events appended by other processes since the last call; they are
        ringed, de-duplicated by ``seq`` and pushed to subscribers."""
        self._ensure_loaded()
        return self._tail_records()

    def _tail_records(self) -> list[Event]:
        if self._tailer is None:
            return []
        known = {e.seq for e in self._ring}
        new: list[Event] = []
        for rec in self._tailer.read_new():
            seq = rec.get("seq")
            if isinstance(seq, int) and (seq in known or seq <= self.latest_seq):
                continue
            ev = self._ingest(rec, notify=True)
            if ev is not None:
                known.add(ev.seq)
                new.append(ev)
        return new

    # -- subscribers ---------------------------------------------------------
    def subscribe(self) -> asyncio.Queue[Event]:
        q: asyncio.Queue[Event] = asyncio.Queue()
        self._subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[Event]) -> None:
        try:
            self._subscribers.remove(q)
        except ValueError:
            pass

    def _notify(self, ev: Event) -> None:
        for q in list(self._subscribers):
            try:
                q.put_nowait(ev)
            except asyncio.QueueFull:
                log.warning("event subscriber queue full; dropping seq %s", ev.seq)
            except RuntimeError:
                # Queue bound to a closed loop; drop the subscriber.
                self.unsubscribe(q)

    # -- prompts -------------------------------------------------------------
    def create_prompt(
        self,
        agent_id: str,
        question: str,
        *,
        node_id: str | None = None,
        kind: str = "text",
        choices: list[str] | None = None,
    ) -> str:
        """Append a ``needs_input`` event and index it; returns the prompt id."""
        if kind not in PROMPT_KINDS:
            raise ValueError(f"invalid prompt kind {kind!r}; expected one of {PROMPT_KINDS}")
        prompt_id = uuid.uuid4().hex[:8]
        while prompt_id in self.pending_prompts:  # pragma: no cover - astronomically rare
            prompt_id = uuid.uuid4().hex[:8]
        self.append(
            agent_id=agent_id,
            node_id=node_id,
            type="needs_input",
            note=question,
            data={
                "prompt_id": prompt_id,
                "question": question,
                "kind": kind,
                "choices": list(choices or []),
            },
        )
        return prompt_id

    def answer_prompt(self, prompt_id: str, value: Any, *, agent_id: str = "user") -> Event:
        """Append a ``reply`` event for ``prompt_id`` and mark it answered."""
        prompt = self.pending_prompts.get(prompt_id)
        if prompt is None:
            raise KeyError(f"unknown prompt_id {prompt_id!r}")
        return self.append(
            agent_id=agent_id,
            node_id=prompt.get("node_id"),
            type="reply",
            note=str(value) if value is not None else "",
            notified=[prompt["agent_id"]] if prompt.get("agent_id") else [],
            data={"prompt_id": prompt_id, "value": value},
        )

    def get_prompt(self, prompt_id: str) -> dict[str, Any] | None:
        return self.pending_prompts.get(prompt_id)

    def _track_prompt(self, ev: Event) -> None:
        prompt_id = ev.data.get("prompt_id") if isinstance(ev.data, dict) else None
        if not isinstance(prompt_id, str) or not prompt_id:
            return
        if ev.type == "needs_input":
            self.pending_prompts[prompt_id] = {
                "agent_id": ev.agent_id,
                "node_id": ev.node_id,
                "question": str(ev.data.get("question") or ev.note),
                "kind": str(ev.data.get("kind") or "text"),
                "choices": list(ev.data.get("choices") or []),
                "answered": False,
                "value": None,
            }
        elif ev.type == "reply":
            prompt = self.pending_prompts.get(prompt_id)
            if prompt is None:
                # Reply whose prompt we never saw (e.g. truncated log): index it anyway.
                prompt = self.pending_prompts[prompt_id] = {
                    "agent_id": None,
                    "node_id": ev.node_id,
                    "question": "",
                    "kind": "text",
                    "choices": [],
                    "answered": False,
                    "value": None,
                }
            prompt["answered"] = True
            prompt["value"] = ev.data.get("value", ev.note)

    # -- introspection -------------------------------------------------------
    def __len__(self) -> int:
        return len(self._ring)
