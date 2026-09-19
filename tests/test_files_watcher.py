import asyncio
import logging
import os
from pathlib import Path

import pytest
from watchdog.events import DirModifiedEvent, FileCreatedEvent, FileModifiedEvent, FileMovedEvent

from whiteboard.files import MarkdownBridge, atomic_write, debounce_worker, start_observer, stop_observer
from whiteboard.files.watcher import is_ignored


def _drain(q: asyncio.Queue[str]) -> list[str]:
    out = []
    while not q.empty():
        out.append(q.get_nowait())
    return out


async def _bridge() -> tuple[MarkdownBridge, asyncio.Queue[str]]:
    loop = asyncio.get_running_loop()
    q: asyncio.Queue[str] = asyncio.Queue()
    return MarkdownBridge(loop, q), q


async def test_bridge_moved_uses_dest_path() -> None:
    b, q = await _bridge()
    b.dispatch(FileMovedEvent("/p/.whiteboard/plan/.hld.md.tmp.123", "/p/.whiteboard/plan/hld.md"))
    await asyncio.sleep(0)
    assert _drain(q) == ["/p/.whiteboard/plan/hld.md"]


async def test_bridge_modified_created_use_src_path() -> None:
    b, q = await _bridge()
    b.dispatch(FileModifiedEvent("/p/.whiteboard/events.jsonl"))
    b.dispatch(FileCreatedEvent("/p/.whiteboard/plan/nodes/node-a.md"))
    await asyncio.sleep(0)
    assert _drain(q) == ["/p/.whiteboard/events.jsonl", "/p/.whiteboard/plan/nodes/node-a.md"]


async def test_bridge_ignores_temp_layout_cache_log_and_directories() -> None:
    b, q = await _bridge()
    b.dispatch(FileModifiedEvent("/p/.whiteboard/plan/.hld.md.tmp.123"))
    b.dispatch(FileMovedEvent("/p/.whiteboard/plan/hld.md", "/p/.whiteboard/plan/.hld.md.tmp.9"))
    b.dispatch(FileModifiedEvent("/p/.whiteboard/plan/hld.layout.json"))
    b.dispatch(FileModifiedEvent("/p/.whiteboard/.cache/last_good.md"))
    b.dispatch(FileModifiedEvent("/p/.whiteboard/server.log"))
    b.dispatch(FileModifiedEvent("/p/.whiteboard/server.json"))
    b.dispatch(FileModifiedEvent("/p/.whiteboard/README.txt"))
    b.dispatch(FileMovedEvent("/p/.whiteboard/plan/hld.md", "/p/.whiteboard/plan/hld.md.bak"))
    b.dispatch(DirModifiedEvent("/p/.whiteboard/plan"))
    b.dispatch(FileModifiedEvent("/p/.whiteboard/plan/.DS_Store"))
    await asyncio.sleep(0)
    assert _drain(q) == []


def test_is_ignored() -> None:
    assert is_ignored("/p/.whiteboard/.cache/x.md")
    assert is_ignored("/p/.whiteboard/plan/hld.layout.json")
    assert is_ignored("/p/.whiteboard/server.log")
    assert is_ignored("/p/.whiteboard/server.log.1")
    assert is_ignored("/p/.whiteboard/plan/.hld.md.tmp.1")
    assert not is_ignored("/p/.whiteboard/plan/hld.md")
    assert not is_ignored("/p/.whiteboard/events.jsonl")


async def test_bridge_bytes_paths_decoded() -> None:
    b, q = await _bridge()
    b.dispatch(FileModifiedEvent(b"/p/.whiteboard/plan/hld.md"))
    await asyncio.sleep(0)
    assert _drain(q) == ["/p/.whiteboard/plan/hld.md"]


async def test_debounce_coalesces_same_path() -> None:
    q: asyncio.Queue[str] = asyncio.Queue()
    calls: list[str] = []

    async def handler(path: str) -> None:
        calls.append(path)

    worker = asyncio.create_task(debounce_worker(q, handler, delay=0.05))
    try:
        for _ in range(5):
            q.put_nowait("/a.md")
            await asyncio.sleep(0.01)
        assert calls == []
        await asyncio.sleep(0.15)
        assert calls == ["/a.md"]
    finally:
        worker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await worker


async def test_debounce_distinct_paths_delivered_separately() -> None:
    q: asyncio.Queue[str] = asyncio.Queue()
    calls: list[str] = []

    async def handler(path: str) -> None:
        calls.append(path)

    worker = asyncio.create_task(debounce_worker(q, handler, delay=0.05))
    try:
        q.put_nowait("/a.md")
        q.put_nowait("/b.md")
        q.put_nowait("/a.md")
        await asyncio.sleep(0.15)
        assert sorted(calls) == ["/a.md", "/b.md"]
        # A later put fires again (trailing edge, not one-shot).
        q.put_nowait("/a.md")
        await asyncio.sleep(0.15)
        assert calls.count("/a.md") == 2
    finally:
        worker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await worker


async def test_debounce_handler_exception_does_not_kill_worker(caplog: pytest.LogCaptureFixture) -> None:
    q: asyncio.Queue[str] = asyncio.Queue()
    calls: list[str] = []

    async def handler(path: str) -> None:
        calls.append(path)
        if path == "/bad.md":
            raise RuntimeError("boom")

    worker = asyncio.create_task(debounce_worker(q, handler, delay=0.02))
    try:
        with caplog.at_level(logging.ERROR, logger="whiteboard.files.watcher"):
            q.put_nowait("/bad.md")
            await asyncio.sleep(0.1)
            q.put_nowait("/good.md")
            await asyncio.sleep(0.1)
        assert calls == ["/bad.md", "/good.md"]
        assert not worker.done()
        assert any("boom" in r.exc_text for r in caplog.records if r.exc_text)
    finally:
        worker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await worker


@pytest.mark.slow
async def test_real_fsevents_observer(tmp_path: Path) -> None:
    loop = asyncio.get_running_loop()
    q: asyncio.Queue[str] = asyncio.Queue()
    observer = start_observer(loop, q, tmp_path)
    try:
        wb = tmp_path / ".whiteboard"
        assert wb.is_dir()
        assert observer.is_alive()
        await asyncio.sleep(0.5)  # FSEvents warm-up
        (wb / "plan").mkdir()
        target = wb / "plan" / "hld.md"
        atomic_write(target, b"# HLD\n")
        # The layout sidecar must never surface.
        (wb / "plan" / "hld.layout.json").write_text("{}")
        seen: set[str] = set()
        deadline = loop.time() + 5.0
        while os.path.realpath(target) not in seen and loop.time() < deadline:
            try:
                seen.add(os.path.realpath(await asyncio.wait_for(q.get(), timeout=deadline - loop.time())))
            except asyncio.TimeoutError:
                break
        assert os.path.realpath(target) in seen
        assert all(not p.endswith(".layout.json") and "tmp" not in os.path.basename(p) for p in seen)
    finally:
        await stop_observer(observer)
    assert not observer.is_alive()
