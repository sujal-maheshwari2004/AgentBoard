# RUNBOOK — the root-agent protocol and the MCP tools, for humans

What Claude does in whiteboard mode, so you can tell when it is doing it right. The authoritative
text Claude follows is `skill/whiteboard/SKILL.md`; the wire formats are in `CONTRACTS.md`.

## 1. Roles

| role | who | talks to |
|---|---|---|
| **root** | the interactive Claude Code session that ran the skill | you, the server (MCP + pushed messages), subagents (Agent tool, SendMessage) |
| **whiteboard-task** subagent | one per dispatched node (`agent-<slug>`) | the server (MCP) only: `set_agent_status`, `report_progress`, `chat_reply`, `write_agent_plan`/`write_agent_diagram`, `append_event`, `ask_user`/`get_reply` |
| **whiteboard-liaison** subagent | read-only look at another project | the peer project's server or files; never mutates anything |
| **server** | `whiteboard _serve` per project | files, websocket clients, the root's inbox socket |

Subagents never message each other. The only channel between them is `events.jsonl`; root routes.

## 2. Entering the mode

1. `bash ~/.claude/skills/whiteboard/scripts/enter.sh "$PWD"` -> one JSON line:
   `{url, mcp_url, port, first_registration, scaffolded, project, session_project,`
   `session_project_matches, inbound_ok, inbound_scope, inbound_sources, agents_installed,`
   `fix_hint, warnings}`.
2. `first_registration: true` -> Claude tells you to run `/mcp` (or restart) and waits until
   `mcp__whiteboard__status` works. It must not continue without it.
3. `scaffolded: true` -> Claude mentions that `.whiteboard/COLLABORATION.md` is editable.
4. Every entry in `warnings` is relayed to you in one line. Claude must not "fix" a warning by
   editing settings files; the only settings change it may suggest is the printed `fix_hint`,
   which you run yourself.
5. `agents_installed.ok: false` -> Claude says which definition is missing or `user-modified`, and
   that dispatch falls back to `subagent_type: general-purpose` (the job spec is self-sufficient).
6. Claude reads `PLAN.md` (`read_plan_md`) and summarises the state in three lines.

## 2.1 Running Claude Code so pushes work

The canvas pushes into **the session you are typing in**, and Claude Code only accepts a push when
`crossSessionInbound` is `"accept"` in the settings *that session* reads: user scope
(`~/.claude/settings.json`) or the session's **own** project (`.claude/settings.json` /
`settings.local.json`). Scaffolding writes the key into the *whiteboarded* project, which is the
wrong file whenever you run the session somewhere else — that is the whole bug behind "canvas edits
never reach Claude".

Two safe options:

- run the session from inside the whiteboarded project (`session_project_matches: true`), or
- set it once at user scope. That is already done on this machine; the original file is backed up
  at `~/.claude/settings.json.bak`.

| `session_project_matches` | `inbound_ok` | `inbound_scope` | what to do |
|---|---|---|---|
| true | true | `user` or `session-project` | nothing; pushes arrive |
| false | true | `user` | nothing; the user-scope key covers this session |
| false | true | `session-project` | fine, but note Claude is whiteboarding a different project than it is running in |
| any | false | `null` | run the printed `fix_hint` yourself and restart Claude Code |

`agents_installed.ok: false` is independent of all of the above: it only costs you the
`whiteboard-task` subagent type. `make install-skill` in `~/AgentBoard` reinstalls both definitions.

**Claude will never edit `~/.claude/settings.json` for you. If it offers to, say no.** While
`inbound_ok` is false the session still works: Claude polls `get_events(since_seq=...)` after every
turn and the `UserPromptSubmit` hook reminds it what is unread.

## 3. What arrives in the root session

The server pushes one message per 300 ms burst of canvas activity:

```
[whiteboard seq=57 project=AgentBoard]
- chat (to: root, thread root): "can we merge parser and files?"
- chat (to: agent-parser, thread agent-parser): "use the fixture corpus in tests/fixtures"
- edit: node-parser renamed to "Mermaid parser v2"
- risky edit accepted (req 3f2a): node-store depends_on -node-files — "files layer is now vendored"
- diagram approved: lld (req 7b10; 6 nodes, 5 edges; created node-lexer) — "split the lexer out"
- dispatch approved: node-parser → agent-parser (spawn it now with subagent_type=whiteboard-task; job spec: .whiteboard/agents/agent-parser/plan.md)
- agent-parser done on node-parser; notified agent-store; now dispatchable: node-store
- needs_input reply (prompt 9c1d, agent-parser): "yes, keep ELK out"
- plan pasted (event 58): structure it with upsert_node and propose_diagram
Use mcp__whiteboard__get_events(since_seq=56) for full payloads.
```

Root acts on **every bullet**, fetching `get_events(since_seq=...)` when a bullet is ambiguous.
The `UserPromptSubmit` hook adds unread events, unread chat per thread and pending diagram
proposals to your prompts as a backstop.

The prefixes below are the machine-readable registry in `CONTRACTS.md` §9; the conformance test
keeps this table, the skill's own table and the code that emits them in sync.

| bullet | root does |
|---|---|
| `plan pasted (event N)` | `upsert_node` per unit of work (`node-<slug>`), then `propose_diagram` for `hld`, `lld` and (only with data entities) `er` |
| `chat (to: root, thread root)` | answers you with `chat_reply("root", ...)`; applies plan changes with `upsert_node` / `set_node_status` / `propose_diagram` |
| `chat (to: agent-x, thread agent-x)` | `SendMessage(to=<agent-x's claude_agent_ref>, ...)` verbatim, telling the agent to answer with `chat_reply`; never answers for the agent |
| `edit: ...` | nothing (cosmetic) unless it contradicts the plan |
| `status: ...` | the server already applied it; re-runs `get_dispatchable` |
| `external edit: ... [path]` / `external edit (risky): ...` | the file is the truth; re-reads it with `read_node` / `read_plan`; re-checks `get_dispatchable` if `depends_on` moved |
| `risky edit accepted (req R)` | nothing to apply (the server did it); re-checks `get_dispatchable` if `depends_on` changed |
| `risky edit rejected (req R)` | nothing to apply (the server reverted it) |
| `needs_input (prompt P, agent-x)` | tells you the question and that the canvas prompt collects the answer; never answers for you |
| `needs_input reply (prompt P, agent-x)` | `SendMessage(to=<ref>, message="Reply to prompt P: <value>")` |
| `dispatch approved: node-x → agent-x` | `Agent(subagent_type="whiteboard-task", prompt=<agents/agent-x/plan.md>, background)`, then immediately `set_agent_ref(agent-x, claude_agent_ref=<handle>)` |
| `dispatch rejected: node-x → agent-x` | `set_agent_status(agent-x, "idle")`; asks what to change |
| `agent-x done on node-x; notified ...; now dispatchable: ...` | `finalize_agent(agent-x, tokens_in=..., tokens_out=..., cost_usd=..., duration_s=...)`, a two-line recap via `chat_reply("root", ...)`, then the next dispatch proposal |
| `agent-x blocked on node-x: ...` | tells you what is blocked and why; proposes a fix or asks via `ask_user`; posts the same two lines with `chat_reply("root", ...)` |
| `diagram approved: <name> (req R)` | the board file is written; `upsert_node` for every id listed under `created ...`, then continues intake |
| `diagram rejected: <name> (req R)` | redrafts and calls `propose_diagram(<same name>, ...)` again, answering your note. Never `write_diagram` |
| `agent plan edited: agent-x — <first line>` | one `SendMessage` telling the agent to re-read its plan and acknowledge with `report_progress(ack_event_seq=N)` |
| `agent diagram edited: agent-x — <first line>` | the same, pointing the agent at `agents/agent-x/diagrams.md` and `write_agent_diagram` |
| `agent-x acknowledged plan edit (event N)` | nothing to do; confirms it to you in one line |

## 3.1 Approving diagrams

Root **proposes**, you approve. On plan intake Claude drafts three boards and calls
`propose_diagram` once per board (`hld` and `lld` always; `er` only when the plan has persisted data
entities — if it skips ER it must say why in one line). Each proposal arrives on the canvas as a
card showing the mermaid, Claude's one-sentence rationale and the node/edge diff (what would be
created, changed or orphaned).

You can:

- **approve** — the file is written and Claude gets `diagram approved: <name> ...` listing the ids
  it now has to flesh out with `upsert_node`;
- **approve with edits** — your mermaid wins; Claude re-reads with `read_plan` and reconciles;
- **reject with a note** — Claude redrafts and proposes the same board again.

Root cannot bypass you: `write_diagram` is forbidden to it, and pending proposals are replayed to
the canvas after a restart, so nothing is silently lost. Dispatched agents never touch the boards;
each writes only its own `agents/<id>/diagrams.md` with `write_agent_diagram`.

## 3.2 Chatting with an agent

There is one `root` thread (you ↔ Claude) plus one thread per agent (you ↔ that agent). Your
message in an agent's thread is forwarded to it **verbatim**; the agent answers with
`chat_reply(from_id=<its id>, text=...)` **in that thread**. Claude never answers on an agent's
behalf, and agents never post in another agent's thread.

If an agent is quiet, look at its card: the activity line is what it says it is doing and
`heartbeat_at` is when it last said anything. A live agent reports at every milestone.

The last 50 messages per thread are replayed when the canvas reconnects, so a browser reload or a
server restart does not lose the conversation.

## 3.3 Editing an agent's plan while it runs

Edit `agents/<id>/plan.md` on the canvas or straight on disk. The server diffs it, logs an
`agent_plan_edited` event N and pushes `agent plan edited: agent-x — <first line>`. Claude then
sends the agent exactly one message; the agent stops, re-reads the whole plan and calls
`report_progress(agent_id, activity=<what changes>, ack_event_seq=N)`, which you see as
`agent-x acknowledged plan edit (event N)`.

Expect the acknowledgement within one agent turn. If it does not arrive before the agent's next
progress report, Claude must say so out loud — an unacknowledged edit means the agent is still
building the old spec. An agent that was never dispatched (no `claude_agent_ref`) or is already
`done` cannot be notified: the edit only takes effect on the next dispatch, and Claude says that
instead of messaging.

## 4. The dispatch loop

```
get_dispatchable  ──►  write job spec (goal, read-first files, deps + interfaces,
                       dependents whose interfaces must stay stable, allowed paths,
                       deliverables, branch)
                  ──►  propose_dispatch(node_id, "agent-<slug>", job_spec_md)
                           │  server writes agents/<id>/plan.md, shows the "Dispatch?" pop-up
                           ▼
                       you approve on the canvas
                           │  "dispatch approved" bullet + dispatch_approved event; node.owner = agent
                           ▼
                       root spawns the whiteboard-task agent, then set_agent_ref(<handle>)
                           │
                       agent: set_agent_status("working") ... append_event("done"|"blocked"|"needs_input")
                           │  done: node -> done, dependents' cards get ready_deps, `notified` lists them,
                           ▼        root gets "now dispatchable: ..."
                       root: finalize_agent(tokens, cost, duration) and back to the top
```

Root never spawns before approval and never edits `PLAN.md`, `server.json` or `*.layout.json` by
hand. Every state change goes through `mcp__whiteboard__*` so files and canvas stay in sync.

## 5. Working with another project

"Work with project X" -> `Agent(subagent_type="whiteboard-liaison", prompt="Peer project: <abs path>. Return its skeleton ...")`.
The liaison uses `get_skeleton(project_path)` / `read_peer_node(project_path, node_id)`, which go
to X's live server via `~/.whiteboard/registry.json` or fall back to reading X's files. Nothing is
ever started or written in X.

## 6. MCP tools (server name `whiteboard`; CONTRACTS §8)

All tools return JSON dicts. Errors come back as tool errors with a readable message.

| tool | params | returns |
|---|---|---|
| `status` | – | `{project, port, rev, nodes, agents, latest_seq, bridge}` |
| `read_plan` | – | `PlanSnapshot` (without layout) |
| `read_node` | `id` | `Node` |
| `read_collaboration` | – | `{text}` |
| `read_plan_md` | – | `{text}` (PLAN.md) |
| `upsert_node` | `id, title, type="hld", status=None, depends_on=None, interfaces=None, body=None, diagram=None` | `Node` — `diagram=None` homes the node on the board named by its `type` |
| `set_node_status` | `id, status, owner=None` | `Node` |
| `delete_node` | `id` | `{deleted: id}` |
| `write_diagram` | `name, mermaid` | `Diagram` — **forbidden to root**; boards change only through `propose_diagram` |
| `propose_diagram` | `name, mermaid, rationale=""` | `{request_id, nodes_added, nodes_removed, edges_added, edges_removed}`; writes nothing until you approve on the canvas |
| `upsert_agent` | `agent_id, assigned_node, status="idle", plan_md=None, diagrams_md=None, claude_agent_ref=None, notes=None` | `AgentCard` |
| `set_agent_ref` | `agent_id, claude_agent_ref, spawned_at=None` | `AgentCard` (appends `spawned`) — root calls it immediately after the Agent tool returns a handle |
| `write_agent_plan` | `agent_id, plan_md` | `AgentCard` — the agent rewrites its own job spec before deviating from it |
| `write_agent_diagram` | `agent_id, mermaid` | `AgentCard` — the agent's own LLD in `agents/<id>/diagrams.md`; free-form box ids |
| `report_progress` | `agent_id, activity=None, progress=None, metrics=None, files_touched=None, final=False, ack_event_seq=None` | `{agent, heartbeat_seq}` — cheap: no `rev` bump, no `PLAN.md` regen |
| `finalize_agent` | `agent_id, tokens_in, tokens_out, cost_usd, duration_s, note=""` | `AgentCard` — root only, from the Agent tool's completion notification |
| `chat_reply` | `from_id, text, thread=None, reply_to=None` | `{message, seq}` — `from_id` is `root` or the agent id; the thread defaults to it |
| `set_agent_status` | `agent_id, status, note=""` | `AgentCard` (also appends `agent_changed` event) |
| `append_event` | `agent_id, node_id, type, note="", data=None` | `Event` — for `done`: sets node status done, marks ready deps on dependents' cards, `notified` = owners of dependents, publishes, pushes root with newly dispatchable nodes |
| `ask_user` | `agent_id, question, choices=None, node_id=None, kind="text"` | `{prompt_id}` (publishes `needs_input`, appends event) |
| `get_reply` | `prompt_id` | `{answered: bool, value?}` |
| `get_dispatchable` | – | `{nodes: [Node]}` |
| `propose_dispatch` | `node_id, agent_id, job_spec_md` | `{request_id}` (writes agent folder with status idle, publishes `dispatch.request`, appends `dispatch_proposed`) |
| `get_events` | `since_seq=0, limit=200` | `{events: [Event], latest_seq}` |
| `wait_for_events` | `since_seq, timeout_s=20` | same as get_events; returns when any new event or timeout (cap 25 s) |
| `get_skeleton` | `project_path=None` | `Skeleton` |
| `read_peer_node` | `project_path, node_id` | `Node` dict |
| `save_layout` | `diagram, patch` | `{ok}` (rarely used by agents) |

Ids: nodes `^node-[a-z0-9][a-z0-9-]*$`, agents `^agent-[a-z0-9][a-z0-9-]*$`. Node statuses
`todo | in_progress | blocked | done`; agent statuses `idle | working | blocked | done`. The 21
event types: `done blocked needs_input reply chat dispatch_proposed dispatch_approved
dispatch_rejected spawned diagram_proposed diagram_approved diagram_rejected risky_edit
risky_edit_accepted risky_edit_rejected plan_pasted node_changed agent_changed agent_plan_edited
heartbeat info`.

## 7. When something looks wrong

| symptom | check |
|---|---|
| Claude says the whiteboard tools are missing | `/mcp`; `.mcp.json` has `whiteboard` pointing at the URL in `.whiteboard/server.json`; `whiteboard status --project-dir .` |
| canvas edits do not reach Claude | `GET /api/health` -> `bridge.ok`/`last_error`; re-run the skill (re-targets the socket) |
| canvas does not follow file edits | `.whiteboard/server.log` for watcher errors; `GET /api/debug/state` -> `invalid` lists unparseable files |
| a dispatch never happens | the pop-up must be approved on the canvas; root waits for the "dispatch approved" bullet |
| canvas events never arrive at all | the `enter.sh` JSON: `inbound_ok` must be true and, when `session_project_matches` is false, the accepting setting must be at user scope or in the *session's* project. Run the printed `fix_hint` yourself and restart Claude Code (§2.1) |
| "unknown subagent type whiteboard-task" | `~/.claude/agents/whiteboard-task.md` is missing or `user-modified`; `make install-skill` in `~/AgentBoard` reinstalls it. Dispatch still works meanwhile: Claude retries with `subagent_type: general-purpose` and the job spec is self-sufficient |
| an agent reports the whiteboard tools are missing | subagents inherit only the MCP servers connected in the root session: run `/mcp` there, then re-dispatch. The agent is told to stop rather than work unrecorded |
| a diagram proposal never appears, or never resolves | `GET /api/health` -> `pending_diagrams`; the `UserPromptSubmit` hook also counts them. Pending proposals are replayed on reconnect; root must never call `write_diagram` to get around one |
| an agent card stops updating | `heartbeat_at` on the card: cards are flushed to disk at most every 5 s, so a few seconds of lag is normal and minutes is not. Check the activity line, then chat with the agent in its thread |
| server will not start | `whiteboard start` prints the log tail; port range 43000-43999 must be reachable on 127.0.0.1 |
| state after restart differs | it cannot: state is only files + `*.layout.json`; compare `git diff .whiteboard/` |
