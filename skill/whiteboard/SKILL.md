---
name: whiteboard
description: Enter whiteboard mode — plan and execute this project visually with Claude on a local tldraw canvas backed by .whiteboard/ markdown. Use when the user says "enter whiteboard mode", "whiteboard", "open the canvas", or wants to plan with diagrams and spawn subagents per task.
allowed-tools: Bash(bash ~/.claude/skills/whiteboard/scripts/*) Read
---

# Whiteboard mode

You are the **root session**: the only process that talks to the user, the canvas server (MCP `whiteboard`) and the subagents. Subagents never talk to each other; everything flows through whiteboard events and you route them.

## Step 1 — enter

Run exactly:

```bash
bash ~/.claude/skills/whiteboard/scripts/enter.sh "$PWD"
```

It syncs the AgentBoard venv, scaffolds `.whiteboard/` (+ `.claude/agents/whiteboard-*.md`, hooks, gitignore), starts the per-project server, re-targets its push bridge at this session's inbox socket, registers the MCP server (`claude mcp add ... whiteboard`), opens the canvas, and prints one JSON line.

## Step 2 — interpret the JSON

`{url, mcp_url, port, first_registration, scaffolded}`

- `first_registration: true` → tell the user: "The whiteboard MCP server was just registered; run `/mcp` to connect it (or restart Claude Code)." Then call `mcp__whiteboard__status`. If the tool is unavailable, stop and wait for the user to run `/mcp`; do not proceed without it.
- Otherwise call `mcp__whiteboard__status` once to confirm the server is reachable; report `url`, node and agent counts.
- `scaffolded: true` → mention that `.whiteboard/COLLABORATION.md` is the protocol and can be edited.
- Read `.whiteboard/PLAN.md` (`mcp__whiteboard__read_plan_md`) and summarise the current state in three lines.

## Step 3 — root protocol

The server pushes canvas activity into this session as messages beginning `[whiteboard seq=N project=...]`, one bullet per event. **On every such message: read every bullet and act on each**, then fetch full payloads with `mcp__whiteboard__get_events(since_seq=<previous seq>)` when a bullet is ambiguous. A `UserPromptSubmit` hook also reports unread counts; treat that the same way.

Handle bullets as follows:

- **plan pasted** → decompose it into nodes: `upsert_node(id, title, type, depends_on, interfaces, body)` per unit of work (ids `node-<slug>`), then `write_diagram("hld", <flowchart TD with node ids and edges>)`. Keep nodes small enough for one agent. Ask the user only about genuine ambiguities.
- **chat (to: root)** → answer the user; if it changes the plan, apply it with `upsert_node` / `set_node_status` / `write_diagram`.
- **chat (to: agent-x)** → forward verbatim: `SendMessage(to=<agent's claude_agent_ref>, message=<text>)`. Never reply on the agent's behalf.
- **edit** → cosmetic; nothing to do unless it contradicts the plan.
- **risky edit accepted/rejected** → the server already applied or reverted it; re-check `get_dispatchable` if `depends_on` changed.
- **needs_input reply (prompt P, agent-x)** → `SendMessage(to=<agent-x's ref>, message="Reply to prompt P: <value>")`.
- **dispatch approved: node-x → agent-x** → spawn and record:
  1. `Agent(subagent_type="whiteboard-task", prompt=<contents of .whiteboard/agents/agent-x/plan.md>, run in background)`.
  2. `mcp__whiteboard__upsert_agent(agent_id="agent-x", assigned_node="node-x", status="working", claude_agent_ref=<the agent name/id returned>)`.
- **dispatch rejected** → `set_agent_status(agent, "idle")`; ask the user what to change, if anything.
- **agent-x done on node-x; now dispatchable: ...** → for each newly dispatchable node, propose the next dispatch (below). Relay the agent's report to the user in two lines.
- **blocked** → tell the user what is blocked and why; propose a fix or `ask_user` from the root.

### Dispatching a node

When `mcp__whiteboard__get_dispatchable` lists a node and the user wants it worked:

1. Write a job spec in markdown (goal, read-first files, dependencies with their interfaces, dependents whose interfaces must stay stable, allowed code paths, deliverables, branch). Base it on `read_node` of the node and its dependencies.
2. `mcp__whiteboard__propose_dispatch(node_id, agent_id="agent-<slug>", job_spec_md=<spec>)`.
3. Wait for the `dispatch approved` bullet from the canvas; never spawn before approval.

### Working with another project

"Work with project X" / "what does project X expose" → `Agent(subagent_type="whiteboard-liaison", prompt="Peer project: <abs path>. Return its skeleton" + specifics)`. Summarise what it returns; create nodes here if the user wants to depend on peer work. Never spawn a liaison to edit anything.

### Hard rules

- Never let subagents talk to each other; never tell one agent about another's work. If two agents must coordinate, update the node files and let the events carry it.
- Never call `SendMessage` to a subagent for anything except forwarded chat and prompt replies.
- Never edit `.whiteboard/PLAN.md`, `server.json` or `*.layout.json` by hand; use MCP tools.
- Every state change goes through `mcp__whiteboard__*` so the canvas and files stay in sync.
- If `mcp__whiteboard__status` fails at any point, tell the user to run `/mcp`, or re-run this skill.
