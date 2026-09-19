"""FastAPI application factory and foreground runner (CONTRACTS §10; layout per
docs/research/python-server.md "Mounting inside FastAPI").

``create_app(root)`` wires the DONE building blocks together: ``PlanStore``,
``EventLog``, the watchdog bridge, the websocket ``Hub``, the ``ClaudeBridge``
and, when ``whiteboard.mcp_server`` is importable (or a ``mcp_factory`` is
injected), the MCP streamable-HTTP app served at ``/mcp``.

Route order matters: API routes and ``/ws`` first, ``/assets`` static mount,
the SPA fallback (``GET /{path}``, which steps aside for ``/mcp``), and the MCP
Starlette app mounted at ``/`` last. A route registered *after* a root mount is
unreachable, which is why the fallback precedes the mount.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import logging.handlers
import os
import socket
from collections.abc import AsyncIterator, Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from whiteboard import config
from whiteboard.files.watcher import debounce_worker, start_observer, stop_observer
from whiteboard.liaison import registry_update
from whiteboard.server import api
from whiteboard.server.bus import ServerContext
from whiteboard.server.handlers import serve_websocket

__all__ = ["create_app", "run_foreground", "setup_logging", "canvas_dist", "MISSING_CANVAS_HTML"]

log = logging.getLogger(__name__)

LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_BACKUPS = 3
TAIL_POLL_S = 1.0
MISSING_CANVAS_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Whiteboard</title>
<style>body{font:15px/1.5 system-ui,sans-serif;max-width:40rem;margin:4rem auto;padding:0 1rem;color:#222}
code{background:#f3f3f3;padding:.1rem .3rem;border-radius:3px}</style></head>
<body><h1>canvas build missing</h1>
<p>The whiteboard server is running, but <code>canvas/dist/</code> was not found.</p>
<p>Build it with <code>pnpm build</code> in <code>canvas/</code>, then reload this page.</p>
<p>Meanwhile: <a href="/api/health">/api/health</a> · <a href="/api/debug/state">/api/debug/state</a></p>
</body></html>
"""


# --------------------------------------------------------------------------- logging
class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        created = datetime.fromtimestamp(record.created, timezone.utc)
        entry: dict[str, Any] = {
            "ts": created.strftime("%Y-%m-%dT%H:%M:%S") + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            entry["exc"] = self.formatException(record.exc_info)
        return json.dumps(entry, ensure_ascii=False, default=str)


_LOG_HANDLERS: dict[str, logging.Handler] = {}


def setup_logging(root: Path, level: int = logging.INFO) -> logging.Handler:
    """JSON-lines ``RotatingFileHandler`` at ``.whiteboard/server.log`` (5 MB x 3),
    installed once per path on the root logger; noisy libraries are funnelled at WARNING."""
    path = config.server_log_path(root)
    key = str(path.resolve()) if path.parent.exists() else str(path)
    handler = _LOG_HANDLERS.get(key)
    if handler is None:
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(path, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUPS, encoding="utf-8")
        handler.setFormatter(_JsonFormatter())
        handler.setLevel(level)
        logging.getLogger().addHandler(handler)
        _LOG_HANDLERS[key] = handler
    root_logger = logging.getLogger()
    if root_logger.level == logging.NOTSET or root_logger.level > level:
        root_logger.setLevel(level)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "mcp", "watchdog", "httpx2", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)
    return handler


# --------------------------------------------------------------------------- canvas
def canvas_dist() -> Path:
    override = os.environ.get("WHITEBOARD_CANVAS_DIST")
    if override:
        return Path(override).expanduser()
    return Path(__file__).resolve().parent.parent.parent / "canvas" / "dist"


def _index_response(dist: Path):
    index = dist / "index.html"
    if index.is_file():
        return FileResponse(index, media_type="text/html")
    return HTMLResponse(MISSING_CANVAS_HTML, status_code=200)


def _safe_child(dist: Path, rel: str) -> Path | None:
    """``dist/rel`` when it stays inside ``dist``; None on traversal."""
    if not rel or "\x00" in rel or any(part in ("..", "") for part in rel.split("/")):
        return None
    try:
        base = dist.resolve()
        candidate = (dist / rel).resolve()
    except OSError:
        return None
    if candidate != base and not candidate.is_relative_to(base):
        return None
    return candidate


# --------------------------------------------------------------------------- factory
def _load_mcp_factory(injected: Callable | None) -> Callable | None:
    if injected is not None:
        return injected
    try:
        from whiteboard.mcp_server import build_mcp_server  # written by T8
    except ImportError:
        return None
    return build_mcp_server


def create_app(root: Path, *, mcp_factory: Callable | None = None) -> FastAPI:
    """Build the server for ``root``. ``mcp_factory(store, log, bus, root) -> MCPServer``
    overrides the default ``whiteboard.mcp_server.build_mcp_server`` (tests)."""
    root = Path(root).resolve()
    # Before the MCP factory: MCPServer.__init__ calls logging.basicConfig with a
    # Rich console handler, which is a no-op once the root logger has ours.
    setup_logging(root)
    ctx = ServerContext(root)
    dist = canvas_dist()

    mcp_server = None
    factory = _load_mcp_factory(mcp_factory)
    if factory is not None:
        try:
            mcp_server = factory(ctx.store, ctx.log, ctx.bus, root)
        except Exception:
            log.exception("MCP server factory failed; continuing without /mcp")
            mcp_server = None
    mcp_app = None
    if mcp_server is not None:
        mcp_app = mcp_server.streamable_http_app(streamable_http_path="/mcp", stateless_http=True, json_response=True)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        setup_logging(root)
        ctx.port = int(getattr(app.state, "port", 0) or os.environ.get("WHITEBOARD_PORT") or ctx.port or 0)
        ctx.load()
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[str] = asyncio.Queue()
        observer = None
        try:
            observer = start_observer(loop, queue, root)
        except Exception:
            log.exception("file watcher could not start; external edits will not be picked up")

        async def on_change(path: str) -> None:
            if os.path.basename(path) == "events.jsonl":
                ctx.log.tail()
                return
            messages = await ctx.store.apply_file_change(path)
            ctx.bus.publish_messages(messages)

        async def poll_tail() -> None:
            while True:
                await asyncio.sleep(TAIL_POLL_S)
                try:
                    ctx.log.tail()
                except Exception:
                    log.exception("events.jsonl tail failed")

        async def event_pump() -> None:
            q = ctx.log.subscribe()
            try:
                while True:
                    ev = await q.get()
                    ctx.bus.publish_event(ev)
            finally:
                ctx.log.unsubscribe(q)

        tasks = [
            loop.create_task(debounce_worker(queue, on_change), name="wb-debounce"),
            loop.create_task(poll_tail(), name="wb-tail-poll"),
            loop.create_task(event_pump(), name="wb-event-pump"),
        ]
        try:
            registry_update(str(root), {"port": ctx.port, "pid": ctx.pid, "started_at": ctx.started_at})
        except Exception:
            log.exception("registry update failed")
        log.info("whiteboard server up for %s on port %s (pid %s)", root, ctx.port, ctx.pid)
        try:
            if mcp_server is not None:
                async with mcp_server.session_manager.run():
                    yield
            else:
                yield
        finally:
            for t in tasks:
                t.cancel()
            for t in tasks:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await t
            with contextlib.suppress(Exception):
                await ctx.bridge.close()
            with contextlib.suppress(Exception):
                await ctx.hub.close_all()
            if observer is not None:
                with contextlib.suppress(Exception):
                    await stop_observer(observer)
            with contextlib.suppress(Exception):
                registry_update(str(root), None)
            log.info("whiteboard server down for %s", root)

    app = FastAPI(title=f"whiteboard: {ctx.project}", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.ctx = ctx
    app.state.store = ctx.store
    app.state.log = ctx.log
    app.state.hub = ctx.hub
    app.state.bridge = ctx.bridge
    app.state.bus = ctx.bus
    app.state.mcp = mcp_server
    app.state.root = root

    app.include_router(api.router)

    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket) -> None:
        await serve_websocket(ctx, ws)

    @app.get("/", include_in_schema=False)
    def index() -> Any:
        return _index_response(dist)

    if (dist / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=str(dist / "assets")), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    def spa_fallback(path: str, request: Request) -> Any:
        if path == "mcp" or path.startswith("mcp/"):
            raise HTTPException(status_code=405, detail="MCP endpoint: POST JSON-RPC to /mcp")
        if path.startswith(("api/", "assets/")) or path in ("api", "assets", "ws", "skeleton"):
            raise HTTPException(status_code=404, detail="not found")
        candidate = _safe_child(dist, path)
        if candidate is None:
            raise HTTPException(status_code=404, detail="not found")
        if candidate.is_file():
            return FileResponse(candidate)
        return _index_response(dist)

    if mcp_app is not None:
        app.mount("/", mcp_app, name="mcp")

    return app


# --------------------------------------------------------------------------- runner
def _bind(root: Path) -> tuple[socket.socket, int]:
    """Bind ``127.0.0.1:$WHITEBOARD_PORT`` (set by the CLI) or a fresh port."""
    env_port = os.environ.get("WHITEBOARD_PORT")
    if env_port:
        try:
            wanted = int(env_port)
        except ValueError:
            wanted = 0
        if wanted:
            sock = config._try_bind(wanted)
            if sock is not None:
                return sock, wanted
            log.warning("WHITEBOARD_PORT=%s is busy; choosing another port", wanted)
    return config.bind_port(root)


def run_foreground(root: Path, *, on_server: Callable[[Any], None] | None = None) -> None:
    """Run uvicorn on the loopback until it is told to exit. Rewrites
    ``server.json`` when the port differs from what the CLI wrote. ``on_server``
    receives the ``uvicorn.Server`` before it starts (tests use it to stop it)."""
    import uvicorn

    root = Path(root).resolve()
    sock, port = _bind(root)
    setup_logging(root)
    info = config.read_server_json(root)
    if not info or info.get("port") != port or info.get("pid") != os.getpid():
        config.write_server_json(root, port)
    os.environ["WHITEBOARD_PORT"] = str(port)

    app = create_app(root)
    app.state.port = port
    app.state.ctx.port = port
    cfg = uvicorn.Config(app, host="127.0.0.1", port=port, log_config=None, lifespan="on", access_log=False)
    server = uvicorn.Server(cfg)
    if on_server is not None:
        on_server(server)
    try:
        server.run(sockets=[sock])
    finally:
        with contextlib.suppress(OSError):
            sock.close()
