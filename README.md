# Whiteboard

A local, per-project visual whiteboard for planning and executing software work with Claude Code.
You say "enter whiteboard mode" in any project, a tldraw canvas opens in your browser, and you and
Claude plan on it together: boxes are units of work, arrows are dependencies, cards are the
subagents doing the work. Every box, arrow and card is a plain markdown file under
`<project>/.whiteboard/`; the canvas is only a view of those files. Edit a file in your editor and
the canvas updates; rename a box on the canvas and one line of one file changes.

Once the plan is settled, Claude decomposes it into a task graph, proposes a dispatch for each
ready leaf, and (after you approve on the canvas) spawns a subagent per task with its own Agent
tool. Subagents never talk to each other; they report through an append-only `events.jsonl`, and
the canvas shows cards flipping to "ready" as dependencies land. Whiteboard is not a standalone
agent runtime: it is a **skill** (enter the mode), an **MCP server** (Claude reads and writes the
plan structurally) and an **interface** (FastAPI + websocket + tldraw). Claude Code itself does the
orchestration, in the interactive session you already have open.

## Requirements

- macOS (the daemon, hooks and scripts assume a Unix socket and `open`; Linux should work but is untested)
- Python 3.13 or 3.14 and [uv](https://docs.astral.sh/uv/)
- Node 22+ and pnpm (only to rebuild the canvas; a built `canvas/dist/` is committed)
- Claude Code >= 2.1.224 (cross-session messaging: the `CLAUDE_CODE_MESSAGING_SOCKET` inbox
  socket is how the canvas pushes into your session)

## Install

```sh
git clone <this repo> ~/AgentBoard
cd ~/AgentBoard
uv sync                                   # Python env (.venv)
cd canvas && pnpm install && pnpm build   # optional: rebuild canvas/dist
cd .. && make install-skill               # symlinks the skill dir + copies the two agent definitions
```

The checkout location is `AGENTBOARD_HOME` (default `~/AgentBoard`). The skill and the per-project
daemon run `uv run --project $AGENTBOARD_HOME whiteboard ...`, so one install serves every project.

## Usage

In any project, in an interactive Claude Code session, say **"enter whiteboard mode"** (or run
`/whiteboard`). The skill runs `skill/whiteboard/scripts/enter.sh "$PWD"`, which:

1. syncs the AgentBoard venv if needed;
2. `whiteboard scaffold`: creates `.whiteboard/` from templates, writes
   `.claude/agents/whiteboard-task.md` and `whiteboard-liaison.md`, merges two hooks
   (`SessionStart`, `UserPromptSubmit`) and `crossSessionInbound: accept` into
   `.claude/settings.local.json`, and appends the runtime files to `.gitignore`;
3. `whiteboard start`: starts a detached per-project server on `127.0.0.1:<port>` and writes
   `.whiteboard/server.json`;
4. `POST /api/session`: points the server's push bridge at this session's inbox socket;
5. `whiteboard register`: `claude mcp add --transport http --scope project whiteboard <url>/mcp`
   (idempotent; writes `.mcp.json`);
6. installs `~/.claude/agents/whiteboard-task.md` and `whiteboard-liaison.md` as stamped
   copies (never symlinks) of `templates/agents/`;
7. opens the canvas in your browser and prints one JSON line
   `{url, mcp_url, port, first_registration, scaffolded, project, session_project,`
   `session_project_matches, inbound_ok, inbound_scope, inbound_sources, agents_installed,`
   `fix_hint, warnings}`. The script never writes `~/.claude/settings.json`: when the inbound
   setting is missing it prints a one-line `fix_hint` for you to run (see `docs/RUNBOOK.md` §2.1).

**First run:** the MCP server was just registered, so Claude will ask you to run `/mcp` (or
restart Claude Code) to connect it. Until `mcp__whiteboard__status` works, nothing else proceeds.

From then on the root session follows the protocol in `skill/whiteboard/SKILL.md`
(summarised in [docs/RUNBOOK.md](docs/RUNBOOK.md)): paste a plan into the canvas box and Claude
structures it into nodes and a diagram; chat with root (or with a selected agent card, forwarded
verbatim); approve or reject dispatches and risky edits in the pop-ups; answer agents' questions in
the needs-input modal. Everything you do on the canvas is appended to `events.jsonl` and pushed to
the session as one debounced message.

## The `.whiteboard/` layout

```
.whiteboard/
├── PLAN.md                  # generated: overview table + full flowchart; never hand-edit
├── COLLABORATION.md         # the protocol every agent follows; humans may edit
├── plan/
│   ├── hld.md               # markdown with ONE ```mermaid flowchart; box ids = node ids
│   ├── er.md
│   ├── hld.layout.json      # canvas positions (sidecar, gitignored)
│   └── nodes/<node-id>.md   # frontmatter: id, type, title, status, owner, depends_on, interfaces
├── agents/<agent-id>/
│   ├── card.md              # assigned_node, status, claude_agent_ref, ready_deps
│   ├── plan.md              # the agent's job spec
│   └── diagrams.md
├── events.jsonl             # append-only log: the only channel between agents
├── server.json              # {pid, port, url, mcp_url, ...} (runtime, gitignored)
├── server.log               # JSON lines (runtime, gitignored)
└── .cache/last_good.json    # last parseable state (runtime, gitignored)
```

`A --> B` in a diagram means **B depends on A**. Frontmatter `depends_on` is authoritative; the
server rewrites the diagram to match. Boxes drawn in a diagram with no node file become
`type: hld, status: todo` nodes (diagram-first editing works). Node bodies are preserved byte-exact.
Exact formats: [docs/CONTRACTS.md](docs/CONTRACTS.md).

## How the pieces fit

```
                     you (browser)                          you (terminal)
                          │                                       │
                   tldraw canvas  ──── ws /ws ────┐         Claude Code session (root)
                   canvas/dist/                    │           │  ▲            │
                                                   ▼           │  │ inbox      │ Agent tool
              ┌────────────────────────────────────────────┐   │  │ socket     ▼
              │ whiteboard server (FastAPI, 127.0.0.1:port)│◄──┘  │       subagents
              │  Hub ── PlanStore ── EventLog ── Bridge ───┼──────┘       (whiteboard-task)
              │   ▲        │  ▲         ▲                  │◄── MCP /mcp ───┘
              │ watchdog   │  │         │                  │
              └────┼───────┼──┼─────────┼──────────────────┘
                   │       ▼  │         ▼
              <project>/.whiteboard/  (markdown, mermaid, events.jsonl)   ◄── your editor
```

- **PlanStore** parses `.whiteboard/` into a snapshot, applies canvas ops and MCP calls, writes
  files atomically, regenerates `PLAN.md` and the diagrams, and keeps a last-known-good copy.
- **watchdog** watches `.whiteboard/`; external edits are re-parsed (150 ms debounce), diffed, and
  broadcast. The server swallows the echo of its own writes.
- **Hub** fans every change out to all websocket clients; **EventLog** appends and replays
  `events.jsonl` (clients reconnect with `lastSeq` and get the gap).
- **MCP** (`/mcp`, streamable HTTP, stateless) is how Claude and its subagents read and mutate the
  plan structurally. Tool table: [docs/RUNBOOK.md](docs/RUNBOOK.md).
- **ClaudeBridge** is the push channel described next.
- **Liaison**: `~/.whiteboard/registry.json` maps project paths to ports so a
  `whiteboard-liaison` agent can read another project's skeleton (`GET /skeleton` if its server is
  alive, else its files). It never starts the other project's server.

## The push channel (canvas -> Claude)

Every Claude Code session (>= 2.1.224) listens on a Unix socket whose path is exported to its Bash
commands as `CLAUDE_CODE_MESSAGING_SOCKET` (token: `CLAUDE_CODE_MESSAGING_TOKEN`). The whiteboard
server inherits both when the skill starts it, and `POST /api/session` (called by `enter.sh` and by
the `SessionStart` hook) re-targets it whenever you open a new session in the project.

Canvas activity (chat, edits, approvals, agents finishing) is folded by a 300 ms debounce into one
newline-delimited JSON message:

```
{"type":"auth","token":"..."}
{"type":"user","text":"[whiteboard seq=57 project=AgentBoard]\n- chat (to: root): \"can we merge parser and files?\"\n- dispatch approved: node-parser → agent-parser (...)\nUse mcp__whiteboard__get_events(since_seq=56) for full payloads."}
```

If the session is idle, Claude Code starts a new turn with it; if Claude is mid-turn, it reads it
between tool calls. No restart, no flags. The `UserPromptSubmit` hook adds an "N unread canvas
events" note as a backstop, and `bridge.status` on the canvas shows whether pushes are succeeding.

**Upgrade path (Channels).** Claude Code's Channels feature (research preview) lets an MCP server
over stdio push `notifications/claude/channel` events straight into the model's context. It needs
`claude --dangerously-load-development-channels server:whiteboard` (a restart) and, for
Team/Enterprise, `channelsEnabled` in managed settings. When it leaves preview, a ~60-line Node
script that tails `GET /api/events?since=` and emits channel notifications replaces the inbox
socket; nothing else changes. See `docs/research/claude-code.md`.

## CLI

```sh
uv run whiteboard start    --project-dir P [--force] [--no-open]  # start or reuse; prints server.json
uv run whiteboard stop     --project-dir P                        # SIGTERM, then SIGKILL; removes server.json
uv run whiteboard status   --project-dir P [--json|--human]       # exit 1 when not healthy
uv run whiteboard scaffold --project-dir P [--force]              # .whiteboard/, agents, hooks, gitignore
uv run whiteboard register --project-dir P                        # claude mcp add (idempotent)
```

`start` picks a deterministic per-project port in 43000-43999 (probing upward if busy) and records
it in `.whiteboard/server.json` and `~/.whiteboard/registry.json`.

## Development

```sh
uv run pytest                 # Python: unit + integration (+ tests/e2e/test_smoke.py, marked slow)
uv run pytest -m "not slow"   # skip the real-daemon and filesystem-watcher tests
cd canvas
pnpm test                     # vitest (jsdom)
pnpm mock                     # fake ws + static server on 127.0.0.1:43999 replaying fixtures
WHITEBOARD_PORT=43999 pnpm dev   # vite on :5173, proxies /ws, /api, /skeleton to that port
```

Point `WHITEBOARD_PORT` at the port in a real project's `.whiteboard/server.json` to develop the
canvas against the Python server. `pnpm build` writes `canvas/dist/` (committed) which the server
serves at `/`; `WHITEBOARD_CANVAS_DIST` overrides its location.

The end-to-end smoke test (`tests/e2e/test_smoke.py`) starts a real daemon on a scratch project,
drives it with a real websocket client, a real MCP client and a fake inbox socket, edits a file on
disk, and restarts the daemon to prove that nothing lives only in memory. It runs in about 5 s.

## Troubleshooting

- `uv run whiteboard status --project-dir .` says whether the server is healthy and where it is.
- `.whiteboard/server.log` is JSON lines (5 MB x 3 rotation); `start` prints its tail when the
  server fails to come up.
- `GET <url>/api/health` returns project, pid, port, node/agent counts, `latest_seq` and the
  bridge status (`ok`, `failures`, `last_error`, `last_pushed_seq`, `socket`).
- `GET <url>/api/debug/state` dumps the full snapshot, invalid files, pending risky edits and
  prompts, bridge state and client count.
- "canvas build missing" at `/`: run `cd canvas && pnpm install && pnpm build`.
- `mcp__whiteboard__*` tools missing in Claude: run `/mcp`; the server must be up (`.mcp.json`
  points at its URL).
- Pushes not arriving (`bridge.ok: false`): the session socket changed; re-run the skill or
  `POST /api/session` with the current `CLAUDE_CODE_MESSAGING_SOCKET`/`TOKEN`.
- A file that fails to parse is skipped, logged, and listed under `invalid` in `/api/debug/state`;
  the rest of the plan keeps working (`.cache/last_good.json` is the last parseable state).

## Deliberately out of scope (v1)

- No standalone agent runtime: no `claude -p`, no Agent SDK; the interactive session spawns
  subagents with its own Agent tool.
- No direct agent-to-agent messaging; everything goes through `events.jsonl` and the root session.
- No Channels transport (documented upgrade path only), no remote or multi-user access: the server
  binds the loopback, accepts only loopback origins and has no auth.
- No persistence in the canvas (no IndexedDB, no `persistenceKey`): files and the layout sidecar are
  the only state.
- No automatic dispatch: every spawn and every risky edit (dependencies, types, interfaces,
  deletions) waits for a human click.
- No cross-project mutation: a liaison reads a peer project's skeleton and never starts its server.
- Mermaid flowcharts only (`flowchart`/`graph` with boxes and arrows); no other diagram kinds.
- No Windows support.
