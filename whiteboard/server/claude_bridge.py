"""Push channel into the root Claude Code session (CONTRACTS §9,
docs/research/claude-code.md §1).

Lines are queued with :meth:`ClaudeBridge.queue_line`; a 300 ms trailing
debounce folds them into ONE inbox message::

    [whiteboard seq=57 project=AgentBoard]
    - chat (to: root): "can we merge parser and files?"
    - edit: node-parser renamed to "Mermaid parser v2"
    Use mcp__whiteboard__get_events(since_seq=41) for full payloads.

posted as newline-delimited JSON on the session's Unix socket: first
``{"type": "auth", "token": ...}`` (when a token is known), then
``{"type": "user", "text": ...}``. Connect/send happen in a worker thread with
a 5 s timeout. Failures never propagate: the lines stay queued (capped at 200)
and go out with the next flush or a backoff retry.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import threading
from collections import deque
from collections.abc import Callable
from typing import Any

__all__ = ["ClaudeBridge", "post_to_session", "format_push"]

log = logging.getLogger(__name__)

SOCKET_ENV = "CLAUDE_CODE_MESSAGING_SOCKET"
TOKEN_ENV = "CLAUDE_CODE_MESSAGING_TOKEN"
DEBOUNCE_S = 0.3
SEND_TIMEOUT_S = 5.0
MAX_QUEUED_LINES = 200
RETRY_MIN_S = 2.0
RETRY_MAX_S = 30.0


def post_to_session(sock_path: str, token: str | None, text: str, *, timeout: float = SEND_TIMEOUT_S) -> None:
    """Blocking: open the inbox socket, send the auth line then the user line."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(sock_path)
        if token:
            s.sendall((json.dumps({"type": "auth", "token": token}) + "\n").encode("utf-8"))
        s.sendall((json.dumps({"type": "user", "text": text}) + "\n").encode("utf-8"))
    finally:
        s.close()


def format_push(project: str, seq: int, lines: list[str], since_seq: int) -> str:
    body = "\n".join(f"- {line}" for line in lines)
    return (
        f"[whiteboard seq={seq} project={project}]\n"
        f"{body}\n"
        f"Use mcp__whiteboard__get_events(since_seq={since_seq}) for full payloads."
    )


class ClaudeBridge:
    """Debounced, fault-tolerant poster to the root session's inbox socket."""

    def __init__(
        self,
        project_name: str,
        socket_path: str | None = None,
        token: str | None = None,
        *,
        seq_provider: Callable[[], int] | None = None,
        debounce_s: float = DEBOUNCE_S,
        timeout_s: float = SEND_TIMEOUT_S,
        max_lines: int = MAX_QUEUED_LINES,
    ) -> None:
        self.project_name = project_name
        self.socket_path = socket_path if socket_path is not None else (os.environ.get(SOCKET_ENV) or None)
        self.token = token if token is not None else (os.environ.get(TOKEN_ENV) or None)
        self.seq_provider = seq_provider or (lambda: 0)
        self.debounce_s = debounce_s
        self.timeout_s = timeout_s
        self.max_lines = max_lines

        self._lines: deque[str] = deque(maxlen=max_lines)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._timer: asyncio.TimerHandle | None = None
        self._lock = asyncio.Lock()
        self._tlock = threading.Lock()
        self._flushing = False

        self.last_pushed_seq = 0
        self.failures = 0
        self.last_error: str | None = None
        self.pushes = 0
        self.dropped = 0
        self._last_ok: bool | None = None  # None until the first attempt

    # -- configuration ---------------------------------------------------------
    def retarget(self, socket_path: str | None, token: str | None = None) -> None:
        """Point at a (new) session socket, e.g. from ``POST /api/session``."""
        changed = (socket_path or None) != self.socket_path
        self.socket_path = socket_path or None
        if token is not None:
            self.token = token or None
        if changed:
            self.failures = 0
            self.last_error = None
            self._last_ok = None
            log.info("bridge retargeted to %s", self.socket_path)
        if self._lines:
            self._schedule(0.0)

    @property
    def ok(self) -> bool:
        if not self.socket_path:
            return False
        return self._last_ok is not False

    def status(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "failures": self.failures,
            "last_error": self.last_error,
            "last_pushed_seq": self.last_pushed_seq,
            "socket": self.socket_path,
            "queued": len(self._lines),
            "pushes": self.pushes,
        }

    # -- queueing --------------------------------------------------------------
    def queue_line(self, line: str) -> None:
        """Add one bullet line; the flush fires ``debounce_s`` after the last add."""
        text = " ".join(str(line).splitlines()).strip()
        if not text:
            return
        with self._tlock:
            if len(self._lines) == self._lines.maxlen:
                self.dropped += 1
            self._lines.append(text)
        self._schedule(self.debounce_s)

    def queue_lines(self, lines: list[str]) -> None:
        for line in lines:
            self.queue_line(line)

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Remember the server's event loop so lines queued from worker threads
        (sync FastAPI routes, watchdog callbacks) still schedule a flush."""
        self._loop = loop
        with self._tlock:
            pending = bool(self._lines)
        if pending:
            self._schedule(self.debounce_s)

    def _schedule(self, delay: float) -> None:
        loop = self._loop
        if loop is None:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                return  # no loop yet; bind_loop() will schedule the pending lines
        if loop.is_closed():
            return
        try:
            on_loop_thread = asyncio.get_running_loop() is loop
        except RuntimeError:
            on_loop_thread = False
        if on_loop_thread:
            self._schedule_on_loop(delay)
        else:
            loop.call_soon_threadsafe(self._schedule_on_loop, delay)

    def _schedule_on_loop(self, delay: float) -> None:
        loop = self._loop or asyncio.get_running_loop()
        if self._timer is not None:
            self._timer.cancel()
        self._timer = loop.call_later(delay, self._fire)

    def _fire(self) -> None:
        self._timer = None
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        task = loop.create_task(self.flush(), name="claude-bridge-flush")
        task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)

    # -- flushing --------------------------------------------------------------
    async def push_now(self) -> bool:
        """Cancel the debounce timer and flush immediately (tests, shutdown)."""
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        return await self.flush()

    async def flush(self) -> bool:
        """Post every queued line as one message. Returns True on success (or
        when nothing was queued); never raises."""
        async with self._lock:
            with self._tlock:
                lines = list(self._lines)
            if not lines:
                return True
            if not self.socket_path:
                self.last_error = "no session socket configured"
                self._last_ok = False
                return False
            seq = int(self.seq_provider() or 0)
            text = format_push(self.project_name, seq, lines, self.last_pushed_seq)
            try:
                await asyncio.to_thread(post_to_session, self.socket_path, self.token, text, timeout=self.timeout_s)
            except Exception as exc:
                self.failures += 1
                self.last_error = f"{type(exc).__name__}: {exc}"
                self._last_ok = False
                log.warning("bridge push failed (%s failures): %s", self.failures, self.last_error)
                self._schedule(min(RETRY_MAX_S, RETRY_MIN_S * (2 ** min(self.failures - 1, 6))))
                return False
            with self._tlock:
                for _ in range(min(len(lines), len(self._lines))):
                    self._lines.popleft()
            self.pushes += 1
            self.last_pushed_seq = max(self.last_pushed_seq, seq)
            self.last_error = None
            self._last_ok = True
            log.info("bridge pushed %d line(s) up to seq %s", len(lines), seq)
            if self._lines:
                self._schedule(self.debounce_s)
            return True

    async def close(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
