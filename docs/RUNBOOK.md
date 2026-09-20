# RUNBOOK — the root-agent protocol and the MCP tools, for humans

What Claude does in whiteboard mode, so you can tell when it is doing it right. The authoritative
text Claude follows is `skill/whiteboard/SKILL.md`; the wire formats are in `CONTRACTS.md`.

## 1. Roles

| role | who | talks to |
|---|---|---|
| **root** | the interactive Claude Code session that ran the skill | you, the server (MCP + pushed messages), subagents (Agent tool, SendMessage) |
| **whiteboard-task** subagent | one per dispatched node (`agent-<slug>`) | the server (MCP) only: `set_agent_status`, `append_event`, `ask_user`/`get_reply` |
| **whiteboard-liaison** subagent | read-only look at another project | the peer project's server or files; never mutates anything |
| **server** | `whiteboard _serve` per project | files, websocket clients, the root's inbox socket |

Subagents never message each other. The only channel between them is `events.jsonl`; root routes.

## 2. Entering the mode

1. `bash ~/.claude/skills/whiteboard/scripts/enter.sh "$PWD"` -> `{url, mcp_url, port, first_registration, scaffolded}`.
2. `first_registration: true` -> Claude tells you to run `/mcp` (or restart) and waits until
   `mcp__whiteboard__status` works. It must not continue without it.
3. `scaffolded: true` -> Claude mentions that `.whiteboard/COLLABORATION.md` is editable.
4. Claude reads `PLAN.md` (`read_plan_md`) and summarises the state in three lines.

## 3. What arrives in the root session

The server pushes one message per 300 ms burst of canvas activity:

```
[whiteboard seq=57 project=AgentBoard]
- chat (to: root): "can we merge parser and files?"
- chat (to: agent-parser): "use the fixture corpus in tests/fixtures"
- edit: node-parser renamed to "Mermaid parser v2"
- risky edit accepted (req 3f2a): node-store depends_on -node-files — "files layer is now vendored"
- dispatch approved: node-parser → agent-parser (spawn it now with subagent_type=whiteboard-task; job spec: .whiteboard/agents/agent-parser/plan.md)
- agent-parser done on node-parser; notified agent-store; now dispatchable: node-store
- needs_input reply (prompt 9c1d, agent-parser): "yes, keep ELK out"
- plan pasted (event 58): structure it with upsert_node/write_diagram
Use mcp__whiteboard__get_events(since_seq=56) for full payloads.
```

Root acts on **every bullet**, fetching `get_events(since_seq=...)` when a bullet is ambiguous.
The `UserPromptSubmit` hook adds "N unread canvas events" to your prompts as a backstop.

| bullet | root does |
|---|---|
| plan pasted | `upsert_node` per unit of work (`node-<slug>`), then `write_diagram("hld", ...)`; asks only about genuine ambiguities |
| chat (to: root) | answers you; applies plan changes with `upsert_node` / `set_node_status` / `write_diagram` |
| chat (to: agent-x) | `SendMessage(to=<agent-x's claude_agent_ref>, message=<text>)` verbatim; never answers for the agent |
| edit | nothing (cosmetic) unless it contradicts the plan |
| risky edit accepted / rejected | nothing to apply (the server did it); re-checks `get_dispatchable` if `depends_on` changed |
| needs_input reply (prompt P, agent-x) | `SendMessage(to=<ref>, message="Reply to prompt P: <value>")` |
| dispatch approved: node-x → agent-x | `Agent(subagent_type="whiteboard-task", prompt=<agents/agent-x/plan.md>, background)` then `upsert_agent(agent-x, node-x, status="working", claude_agent_ref=<handle>)` |
| dispatch rejected | `set_agent_status(agent-x, "idle")`; asks what to change |
| agent-x done on node-x; now dispatchable: ... | relays the report in two lines; proposes the next dispatch for each newly dispatchable node |
| blocked | tells you what is blocked and why; proposes a fix or asks via `ask_user` |

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
                       root spawns the whiteboard-task agent, records claude_agent_ref
                           │
                       agent: set_agent_status("working") ... append_event("done"|"blocked"|"needs_input")
                           │  done: node -> done, dependents' cards get ready_deps, `notified` lists them,
                           ▼        root gets "now dispatchable: ..."
                       back to the top
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
| `upsert_node` | `id, title, type="hld", status=None, depends_on=None, interfaces=None, body=None, diagram="hld"` | `Node` |
| `set_node_status` | `id, status, owner=None` | `Node` |
| `delete_node` | `id` | `{deleted: id}` |
| `write_diagram` | `name, mermaid` | `Diagram` |
| `upsert_agent` | `agent_id, assigned_node, status="idle", plan_md=None, diagrams_md=None, claude_agent_ref=None, notes=None` | `AgentCard` |
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
`todo | in_progress | blocked | done`; agent statuses `idle | working | blocked | done`. Event types:
`done blocked needs_input reply chat dispatch_proposed dispatch_approved dispatch_rejected spawned
risky_edit risky_edit_accepted risky_edit_rejected plan_pasted node_changed agent_changed info`.

## 7. When something looks wrong

| symptom | check |
|---|---|
| Claude says the whiteboard tools are missing | `/mcp`; `.mcp.json` has `whiteboard` pointing at the URL in `.whiteboard/server.json`; `whiteboard status --project-dir .` |
| canvas edits do not reach Claude | `GET /api/health` -> `bridge.ok`/`last_error`; re-run the skill (re-targets the socket) |
| canvas does not follow file edits | `.whiteboard/server.log` for watcher errors; `GET /api/debug/state` -> `invalid` lists unparseable files |
| a dispatch never happens | the pop-up must be approved on the canvas; root waits for the "dispatch approved" bullet |
| server will not start | `whiteboard start` prints the log tail; port range 43000-43999 must be reachable on 127.0.0.1 |
| state after restart differs | it cannot: state is only files + `*.layout.json`; compare `git diff .whiteboard/` |
