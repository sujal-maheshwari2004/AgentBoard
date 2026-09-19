# Research: Python server stack on Python 3.14 (macOS arm64)

Everything marked ✅ was executed on this machine (Python 3.14.7, uv 0.12.5, Darwin 27 arm64) on 2026-09-20.

## Versions (all installed and exercised on 3.14)

| Package | Version | 3.14 status |
|---|---|---|
| fastapi | 0.141.1 | ✅ pure |
| starlette | 1.6.0 | ✅ (1.x API) |
| uvicorn[standard] | 0.53.0 | ✅ uvloop 0.22.1 / httptools 0.8.0 / websockets 17.1 / watchfiles 1.2.0 all have cp314 wheels; uvloop is auto-selected |
| pydantic / pydantic-core | 2.13.5 / 2.46.5 | ✅ |
| mcp (official SDK) | 2.2.0 | ✅ v2 = breaking rewrite (see below) |
| httpx2 | 2.13.0 | ✅ **`httpx` is not installed by this stack; starlette 1.6 and mcp 2.2 use `httpx2`** |
| pyyaml | 6.0.3 | ✅ cp314 |
| watchdog | 6.0.0 | ⚠️ no cp314 wheel; builds from sdist in ~3 s with Xcode CLT; FSEvents extension loads; `Observer()` → `FSEventsObserver` ✅ |
| typer | 0.27.2 | ✅ |
| pytest / pytest-asyncio | 9.1.1 / 1.4.0 | ✅ |
| python-frontmatter | 1.3.0 | works but NOT used (alphabetizes keys on dump) |
| fastmcp (jlowin) | 4.0.5 | not used |

`requires-python = ">=3.13,<3.15"`; escape hatch `uv python install 3.13 && uv sync --python 3.13` (watchdog has cp313 wheels). Alternative watcher: `watchfiles` (already a transitive dep, cp314 wheels, async `awatch`, no rename pairing). We use watchdog per PRD.

uv: `uv sync --frozen` for skill startup. **uv owns `--project <dir>`; our CLI flag is `--project-dir`.** Invocation: `uv run --project ~/AgentBoard --frozen whiteboard start --project-dir "$PWD"`.

## MCP SDK v2 (`mcp` 2.x) — what changed

| v1 | v2 |
|---|---|
| `from mcp.server.fastmcp import FastMCP` | `from mcp.server import MCPServer` |
| `FastMCP("x", port=9000)` | host/port/transport moved to `.run()` / `streamable_http_app()` |
| `ctx.fastmcp`, `get_context()` | `ctx: Context` parameter |
| `ClientSession` + transports | `from mcp.client import Client; async with Client(url_or_server) as c:` |
| camelCase attrs | snake_case (`is_error`, `input_schema`) |

✅ Verified signature: `MCPServer.streamable_http_app(*, streamable_http_path='/mcp', json_response=False, stateless_http=False, event_store=None, retry_interval=None, max_request_body_size=4194304, session_idle_timeout=1800, max_sessions=10000, transport_security=None, host='127.0.0.1') -> Starlette`.

### Mounting inside FastAPI (✅ verified layout)
```python
from mcp.server import MCPServer
mcp = MCPServer("whiteboard", version="0.1.0")
mcp_app = mcp.streamable_http_app(streamable_http_path="/mcp", stateless_http=True, json_response=True)  # module scope

@contextlib.asynccontextmanager
async def lifespan(app):
    async with mcp.session_manager.run():   # REQUIRED: mounting disables the sub-app lifespan
        yield

app = FastAPI(lifespan=lifespan)
# ... /ws, /api/*, /skeleton, GET /, /assets mount ...
app.mount("/", mcp_app)                     # LAST; serves /mcp
# optional SPA fallback @app.get("/{full_path:path}") registered after the mount, with path-traversal check
```
Traps: `streamable_http_path="/mcp"` + `mount("/mcp")` → `/mcp/mcp`; `StaticFiles` at `/` answers `405` to `POST /mcp`; `session_manager` raises if accessed before `streamable_http_app()`.
`stateless_http=True` + `json_response=True`: no sessions, plain JSON. If a tool later needs `ctx.report_progress()`, set `json_response=False`.
Long-poll tools: `async def`, `await asyncio.wait_for(queue.get(), timeout)`; ✅ two 3 s calls + one fast call completed in 3.18 s total. Claude Code idle timeout is 5 min for HTTP; cap at 25 s. Result size: `MAX_MCP_OUTPUT_TOKENS` 25k default; per-tool `_meta={"anthropic/maxResultSizeChars": N}`.
In-process test: `async with Client(mcp) as c: r = await c.call_tool("read_plan", {...}); r.content[0].text` ✅. Use `structured_output=True` on `@mcp.tool()` to assert `structured_content`.

Registration: `claude mcp add --transport http --scope project whiteboard "http://127.0.0.1:${PORT}/mcp"` → `.mcp.json`.

## Watchdog on macOS (✅ measured)
- Auto `Observer()` → FSEvents. Don't force kqueue/polling.
- Atomic replace (`tmp` → `os.replace`) surfaces as ONE `moved` event: `src_path` = temp, `dest_path` = real file. `PatternMatchingEventHandler(patterns=['*.md'])` matches if either side matches → read `dest_path` for moves and skip non-`.md` paths.
- Plain write of a new file: `modified` arrived BEFORE `created`. Never infer lifecycle from order; always re-read from disk.
- Bridge to asyncio: handler runs on watchdog's thread and only does `loop.call_soon_threadsafe(queue.put_nowait, path)`; capture the loop inside lifespan with `asyncio.get_running_loop()`.
- Trailing per-path debounce 150 ms.
- Shutdown: `observer.stop()`, then `await asyncio.to_thread(observer.join)`.
- Echo loop prevention: registry `path → sha256(bytes we wrote)`; on change, read bytes, if digest matches pop and ignore once; otherwise treat as external edit. Plus semantic compare against last-known-good to swallow whitespace-only reformatting.
- Tests: call the handler directly with `FileMovedEvent`; one `@pytest.mark.slow` real test with 0.5 s warm-up; compare with `os.path.realpath` (`/var/folders` → `/private/var/folders`).

## Atomic writes and JSONL (✅ measured)
```python
def atomic_write(path, data: bytes, *, fsync=True):
    tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}")   # same dir, non-.md suffix
    fd = os.open(tmp, os.O_WRONLY|os.O_CREAT|os.O_TRUNC, 0o644)
    try: os.write(fd, data); fsync and os.fsync(fd)
    finally: os.close(fd)
    os.replace(tmp, path)
    if fsync: dfd = os.open(path.parent, os.O_RDONLY); os.fsync(dfd); os.close(dfd)
```
Cost 0.21 ms with both fsyncs. Always fsync.

JSONL append: `os.open(path, O_WRONLY|O_APPEND|O_CREAT)`, `fcntl.flock(LOCK_EX)`, exactly ONE `os.write(line)`. `PIPE_BUF` (512) is irrelevant for regular files; ✅ 8 processes × 400 records × up to 200 KB → zero corruption. Never buffered `open('a')`.

Tailer: byte offset + inode + partial-line buffer; reset on inode change or size shrink; skip torn lines. Drive from watcher (`*.jsonl`) plus 1 s poll fallback.

## Frontmatter — write our own splitter
`python-frontmatter` `dumps()` alphabetizes keys (top-level and nested) and `post.content` strips leading/trailing whitespace → whole-file diffs. Use:
```python
def split_frontmatter(text) -> tuple[dict, str]  # body byte-exact; {} if no/invalid frontmatter
def join_frontmatter(meta, body) -> str          # yaml.safe_dump(sort_keys=False, default_flow_style=False, allow_unicode=True, width=100, indent=2)
```
Format: `---\n<yaml>---\n\n<body>`.

## FastAPI websocket hub
Per-client bounded `asyncio.Queue(256)` + one writer task per socket (sole sender). `broadcast()` is sync `put_nowait`; `QueueFull` evicts the client, which resyncs via `GET /api/events?since=`. Lifespan owns observer, debounce worker, tailer, registry (de)registration, server.json, and the MCP session manager (innermost).

## Per-project isolation
- Port: `43000 + int.from_bytes(blake2b(path, digest_size=8)) % 1000`, probe 64 upward, else `bind(('127.0.0.1', 0))`. Bind first, pass the socket to `uvicorn.Server(config).run(sockets=[sock])` with `uvicorn.Config(app, fd=sock.fileno(), log_config=None, lifespan='on')`. Never Python `hash()` (salted per process).
- `.whiteboard/server.json`: `{pid, port, started_at, project, url, mcp_url, version:1}` written atomically.
- Liveness: `os.kill(pid, 0)` (ESRCH → dead, EPERM → alive) AND `GET /api/health` whose `project` matches (PID recycling).
- Registry `~/.whiteboard/registry.json`: `{project_path: {port, pid, started_at}}` read-modify-write under `flock`, deregister in lifespan finally, prune dead entries on read. Peers fetched with `httpx2.AsyncClient(timeout=2.0)` + `gather`, failures swallowed.
- Daemonize: `subprocess.Popen([...,'_serve','--project-dir',root], stdin=DEVNULL, stdout=<server.log file>, stderr=STDOUT, start_new_session=True, close_fds=True, env={**os.environ, PYTHONUNBUFFERED:'1'})`. Never a pipe (deadlocks at 64 KB). `start` polls health up to 5 s before printing JSON. Shell equivalent: `setsid ... </dev/null >>server.log 2>&1 & disown`, then poll `/api/health`.
- CLI (typer): `start [--force]`, `stop` (SIGTERM, 5 s, SIGKILL; remove server.json; deregister), `status --json`, `scaffold`, `register`, hidden `_serve`.

## Risky-edit diff
```python
@dataclass(frozen=True)
class NodeSemantics: node_id: str; type: str; status: str; depends_on: frozenset[str]; interfaces: tuple[tuple[str,str],...]
```
`diff_node(old, new) -> list[Change(field, added, removed, before, after, weight)]`; weights: type 100, depends_on 40 per changed edge, interfaces 30 per changed entry, status 5; removals ×2; `is_risky` if score ≥ 30. Graph checks: unknown deps, cycles via iterative DFS. Persist last-known-good to `.whiteboard/.cache/last_good.json` `{version, saved_at, nodes{...}, mtimes{path: mtime}}`; in-memory authoritative; on startup, re-baseline files whose mtime changed while down instead of flagging.

## Testing
- `pytest` config: `asyncio_mode = "auto"`, `asyncio_default_fixture_loop_scope = "function"`.
- `from httpx2 import AsyncClient, ASGITransport` (no lifespan) vs `with TestClient(app) as c:` (runs lifespan; `c.websocket_connect('/ws')` is sync → plain `def` tests). ✅ both verified.
- MCP: in-memory `Client(mcp)`.
- Watchdog: handler unit tests + one slow real test.

## Logging / debug endpoints
JSON-lines `RotatingFileHandler` (5 MB × 3) at `.whiteboard/server.log`, `uvicorn.Config(log_config=None)`, funnel `uvicorn.*`, `mcp`, `watchdog` (WARNING) into it. Endpoints: `GET /api/health {project, pid, port, uptime, clients, nodes, latest_seq, bridge:{ok, failures}}`, `GET /api/events?since=&limit=` (ring buffer of 2000, fallback scan of events.jsonl), `GET /api/debug/state` (parsed state + last-known-good side by side).

## Gotchas checklist
1. `FastMCP` is gone → `MCPServer`. 2. `httpx` → `httpx2`. 3. Starlette 1.6. 4. Mount mcp app at `/` last. 5. No `StaticFiles` at `/`. 6. Run `mcp.session_manager.run()` in lifespan. 7. `stateless_http`/`json_response` are `streamable_http_app()` kwargs. 8. Own frontmatter splitter. 9. One `os.write` per JSONL record. 10. `blake2b` for ports. 11. Moved events: use `dest_path`. 12. FSEvents reorders. 13. `observer.join` via `to_thread`. 14. `call_soon_threadsafe`. 15. Daemon stdout to a file. 16. `--project-dir` not `--project`. 17. `ASGITransport` has no lifespan. 18. `/var` vs `/private/var`. 19. HTTP MCP idle timeout 5 min. 20. Single uvicorn worker only.
