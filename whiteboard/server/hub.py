"""Websocket hub: one bounded queue and one writer task per client (CONTRACTS §6,
docs/research/python-server.md "FastAPI websocket hub").

The writer task is the *sole* sender on a socket, so handlers and background
tasks never race on ``ws.send_*``. ``broadcast``/``send`` are synchronous
``put_nowait`` calls; a full queue evicts the client, which resyncs through
``client.hello`` (``lastSeq``) or ``GET /api/events?since=``.

Envelope: ``{"type", "payload", "seq", "ts", "replyTo"}``. ``seq`` is the event
seq for ``event.append``; every other message gets a per-connection counter
starting at :data:`NON_EVENT_SEQ_BASE` so the canvas never confuses it with an
event (it dedupes ``event.append`` by ``seq < 1_000_000``).
"""

from __future__ import annotations

import asyncio
import itertools
import logging
from typing import Any

from starlette.websockets import WebSocket, WebSocketState

from whiteboard.events.log import now_iso

__all__ = ["Hub", "Connection", "envelope", "NON_EVENT_SEQ_BASE", "QUEUE_SIZE"]

log = logging.getLogger(__name__)

NON_EVENT_SEQ_BASE = 1_000_000
QUEUE_SIZE = 256


def envelope(type: str, payload: Any, seq: int | None = None, reply_to: int | None = None) -> dict:
    """Build a server -> client envelope. ``seq`` is stamped by the connection
    when ``None`` (see :meth:`Connection.enqueue`)."""
    return {"type": type, "payload": payload, "seq": seq, "ts": now_iso(), "replyTo": reply_to}


class Connection:
    """One websocket client: its queue, its non-event seq counter and identity."""

    _ids = itertools.count(1)

    def __init__(self, ws: WebSocket, maxsize: int = QUEUE_SIZE) -> None:
        self.ws = ws
        self.id = next(Connection._ids)
        self.client_id: str | None = None
        self.queue: asyncio.Queue[dict | None] = asyncio.Queue(maxsize=maxsize)
        self._seq = itertools.count(NON_EVENT_SEQ_BASE)
        self.writer: asyncio.Task[None] | None = None
        self.closed = False
        self.sent = 0

    def next_seq(self) -> int:
        return next(self._seq)

    def enqueue(self, message: dict) -> bool:
        """Complete the envelope (stamp ``seq`` when missing) and queue it.
        Returns False when the queue is full (the caller evicts)."""
        if self.closed:
            return False
        msg = dict(message)
        if msg.get("seq") is None:
            msg["seq"] = self.next_seq()
        msg.setdefault("ts", now_iso())
        msg.setdefault("replyTo", None)
        msg.setdefault("payload", None)
        try:
            self.queue.put_nowait(msg)
        except asyncio.QueueFull:
            return False
        return True

    async def run_writer(self) -> None:
        """Drain the queue onto the socket until closed or a ``None`` sentinel."""
        try:
            while True:
                msg = await self.queue.get()
                if msg is None:
                    return
                if self.ws.application_state != WebSocketState.CONNECTED:
                    return
                await self.ws.send_json(msg)
                self.sent += 1
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # socket gone; the reader loop notices too
            log.debug("writer for connection %s stopped: %s", self.id, exc)

    def close_queue(self) -> None:
        self.closed = True
        try:
            self.queue.put_nowait(None)
        except asyncio.QueueFull:
            # Writer is behind anyway; cancelling the task handles it.
            pass


class Hub:
    """Registry of live connections plus fan-out helpers."""

    def __init__(self, maxsize: int = QUEUE_SIZE) -> None:
        self.maxsize = maxsize
        self._conns: dict[int, Connection] = {}
        self.evicted = 0

    # -- lifecycle -------------------------------------------------------------
    def register(self, ws: WebSocket) -> Connection:
        """Add an accepted socket and start its writer task."""
        conn = Connection(ws, self.maxsize)
        conn.writer = asyncio.get_running_loop().create_task(conn.run_writer(), name=f"ws-writer-{conn.id}")
        self._conns[conn.id] = conn
        return conn

    async def unregister(self, conn: Connection) -> None:
        self._conns.pop(conn.id, None)
        conn.close_queue()
        if conn.writer is not None and not conn.writer.done():
            conn.writer.cancel()
            try:
                await conn.writer
            except (asyncio.CancelledError, Exception):
                pass

    def _evict(self, conn: Connection) -> None:
        """Drop a client whose queue overflowed; its socket is closed from a task."""
        if conn.id not in self._conns:
            return
        self.evicted += 1
        log.warning("evicting websocket client %s (%s): send queue full", conn.id, conn.client_id)
        self._conns.pop(conn.id, None)
        conn.close_queue()
        if conn.writer is not None:
            conn.writer.cancel()

        async def _close() -> None:
            try:
                await conn.ws.close(code=1013, reason="send queue full; reconnect")
            except Exception:
                pass

        try:
            asyncio.get_running_loop().create_task(_close())
        except RuntimeError:
            pass

    # -- sending ---------------------------------------------------------------
    def send(self, conn: Connection | WebSocket, message: dict) -> bool:
        """Queue ``message`` (``{"type", "payload", "seq"?, "replyTo"?}``) for one client."""
        target = conn if isinstance(conn, Connection) else self.find(conn)
        if target is None:
            return False
        if not target.enqueue(message):
            self._evict(target)
            return False
        return True

    def broadcast(self, message: dict, exclude: Connection | WebSocket | int | None = None) -> int:
        """Queue ``message`` for every client except ``exclude``; returns the count."""
        skip = exclude.id if isinstance(exclude, Connection) else (self.find(exclude).id if isinstance(exclude, WebSocket) and self.find(exclude) else exclude)
        n = 0
        for conn in list(self._conns.values()):
            if conn.id == skip:
                continue
            if conn.enqueue(message):
                n += 1
            else:
                self._evict(conn)
        return n

    # -- introspection ---------------------------------------------------------
    def find(self, ws: WebSocket) -> Connection | None:
        for conn in self._conns.values():
            if conn.ws is ws:
                return conn
        return None

    @property
    def clients(self) -> int:
        return len(self._conns)

    def connections(self) -> list[Connection]:
        return list(self._conns.values())

    async def close_all(self) -> None:
        for conn in list(self._conns.values()):
            await self.unregister(conn)
            try:
                await conn.ws.close(code=1001, reason="server shutting down")
            except Exception:
                pass
