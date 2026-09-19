"""watchdog -> asyncio bridge with per-path trailing debounce (CONTRACTS §2)."""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable
from pathlib import Path

from watchdog.events import FileSystemEvent, PatternMatchingEventHandler
from watchdog.observers import Observer

__all__ = ["MarkdownBridge", "start_observer", "debounce_worker", "stop_observer", "is_ignored"]

log = logging.getLogger(__name__)

PATTERNS = ["*.md", "*.jsonl"]


def _as_str(p: bytes | str) -> str:
    return os.fsdecode(p) if isinstance(p, bytes) else p


def is_ignored(path: str) -> bool:
    """Paths the bridge never forwards: cache, layout sidecars, the server log,
    and dotfiles (our own ``.name.tmp.<pid>`` temp files among them)."""
    if "/.cache/" in path or path.endswith(".layout.json") or "server.log" in path:
        return True
    return os.path.basename(path).startswith(".")


class MarkdownBridge(PatternMatchingEventHandler):
    """Runs on watchdog's thread; only hands the path to the asyncio loop.

    patterns ['*.md', '*.jsonl']; ignores paths containing '/.cache/',
    '.layout.json', 'server.log', temp files starting with '.'.
    on_any_event -> loop.call_soon_threadsafe(queue.put_nowait, dest_or_src_path)
    """

    def __init__(self, loop: asyncio.AbstractEventLoop, queue: asyncio.Queue[str]) -> None:
        super().__init__(patterns=PATTERNS, ignore_directories=True)
        self.loop = loop
        self.queue = queue

    def on_any_event(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        path = _as_str(event.dest_path) if event.event_type == "moved" else _as_str(event.src_path)
        if not path or is_ignored(path):
            return
        # A moved event passes the pattern filter if *either* side matches; the
        # destination must match on its own (e.g. `foo.md` -> `foo.md.bak`).
        if not any(Path(path).match(pat) for pat in PATTERNS):
            return
        try:
            self.loop.call_soon_threadsafe(self.queue.put_nowait, path)
        except RuntimeError:
            # Loop already closed during shutdown; nothing to deliver to.
            pass


def start_observer(loop: asyncio.AbstractEventLoop, queue: asyncio.Queue[str], root: Path) -> Observer:
    """Watch ``root/.whiteboard`` recursively (creating it if missing) and start."""
    target = Path(root) / ".whiteboard"
    target.mkdir(parents=True, exist_ok=True)
    observer = Observer()
    observer.schedule(MarkdownBridge(loop, queue), str(target), recursive=True)
    observer.start()
    return observer


async def debounce_worker(
    queue: asyncio.Queue[str],
    handler: Callable[[str], Awaitable[None]],
    delay: float = 0.15,
) -> None:
    """Per-path trailing debounce: call ``handler(path)`` once ``delay`` seconds
    after the last put for that path. Runs forever; handler exceptions are
    logged, never propagated."""
    pending: dict[str, asyncio.TimerHandle] = {}
    tasks: set[asyncio.Task[None]] = set()
    loop = asyncio.get_running_loop()

    async def run(path: str) -> None:
        try:
            await handler(path)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("file change handler failed for %s", path)

    def fire(path: str) -> None:
        pending.pop(path, None)
        task = loop.create_task(run(path))
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    try:
        while True:
            path = await queue.get()
            try:
                timer = pending.pop(path, None)
                if timer is not None:
                    timer.cancel()
                pending[path] = loop.call_later(delay, fire, path)
            finally:
                queue.task_done()
    finally:
        for timer in pending.values():
            timer.cancel()


async def stop_observer(observer: Observer) -> None:
    """Stop the observer and join its thread without blocking the loop."""
    observer.stop()
    await asyncio.to_thread(observer.join)
