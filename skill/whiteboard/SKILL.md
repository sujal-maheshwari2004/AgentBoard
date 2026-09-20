---
name: whiteboard
description: Enter whiteboard mode — plan and execute this project visually with Claude on a local tldraw canvas backed by .whiteboard/ markdown. Use when the user says "enter whiteboard mode", "whiteboard", "open the canvas", or wants to plan with diagrams and spawn subagents per task.
allowed-tools: Bash(bash ~/.claude/skills/whiteboard/scripts/*) Read
---

# Whiteboard mode

You are the **root session**: the only process that talks to the owner, the canvas server (MCP `whiteboard`) and the subagents. Subagents never talk to each other; everything flows through whiteboard events and you route them.

## Step 1 — enter

Run exactly:

```bash
bash ~/.claude/skills/whiteboard/scripts/enter.sh "$PWD"
```

It syncs the AgentBoard venv, scaffolds `.whiteboard/` (+ project `.claude/agents/`, hooks, gitignore), installs the two agent definitions at user scope, starts the per-project server, re-targets its push bridge at this session's inbox socket, registers the MCP server, opens the canvas, and prints one JSON line.

## Step 2 — interpret the JSON, then fix what it reports

`{url, mcp_url, port, first_registration, scaffolded, project, session_project, session_project_matches, inbound_ok, inbound_scope, agents_installed, fix_hint, warnings}`

- `first_registration: true` → tell the owner: "The whiteboard MCP server was just registered; run `/mcp` to connect it (or restart Claude Code)." Then call `mcp__whiteboard__status`. If the tool is unavailable, stop and wait; do not proceed without it.
- Otherwise call `mcp__whiteboard__status` once; report `url`, node and agent counts.
- `session_project_matches: false` → **say this out loud**: "This session's project is `<session_project>` but the whiteboard is `<project>`. Canvas pushes are delivered to *this* session, so the inbound setting must exist at user scope or in `<session_project>/.claude/`." Then apply the `inbound_ok` rule below. Never `cd`; never re-scaffold the other project to "fix" it.
- `inbound_ok: false` → warn that **canvas events will not arrive**, print `fix_hint` verbatim in a fenced block, and tell the owner to run it and restart Claude Code. **Never edit `~/.claude/settings.json` yourself, and never propose an edit tool call for it.** Until it is fixed, poll: after every owner turn call `mcp__whiteboard__get_events(since_seq=<last you saw>)`.
- `agents_installed.ok: false` → say which definition is missing or `user-modified` and that dispatch will fall back to `general-purpose` (below).
- `scaffolded: true` → mention that `.whiteboard/COLLABORATION.md` is the protocol and is editable.
- Any `warnings` → relay each in one line.
- Read `.whiteboard/PLAN.md` (`mcp__whiteboard__read_plan_md`) and summarise the state in three lines.
- If `mcp__whiteboard__status` ever fails afterwards: say the MCP connection dropped, tell the owner to run `/mcp`, and if that fails re-run this skill. Do not fabricate state, do not fall back to editing `.whiteboard/` by hand, and do not spawn anything while it is down.

## Step 3 — the push protocol

The server pushes canvas activity into this session as messages beginning `[whiteboard seq=N project=...]`, one bullet per event. **On every such message: read every bullet and act on each**, then fetch full payloads with `mcp__whiteboard__get_events(since_seq=<previous seq>)` when a bullet is ambiguous. The `UserPromptSubmit` hook reports unread events, unread chat per thread and pending diagram proposals; treat it the same way.

### Bullet → action

| bullet | do this |
|---|---|
| `plan pasted …` | run **Plan intake** below |
| `chat (to: root, thread root): "…"` | answer in the thread: `chat_reply("root", <answer>)`. Also say it in your own reply to the owner. If it changes the plan, apply it (`upsert_node` / `set_node_status` / `propose_diagram`) and say so in the same `chat_reply` |
| `chat (to: agent-x, thread agent-x): "…"` | forward verbatim: `SendMessage(to=<agent-x's claude_agent_ref>, message='Owner chat: "<text>". Answer it on the canvas with mcp__whiteboard__chat_reply("agent-x", <your answer>).')`. Never answer for the agent |
| `edit: …` | cosmetic; act only if it contradicts the plan |
| `status: …` | the server applied it; re-run `get_dispatchable` |
| `external edit: … [path]` / `external edit (risky): …` | the file is already the truth; re-read it with `read_node`/`read_plan`; re-check `get_dispatchable` if `depends_on` moved |
| `risky edit accepted (req R): …` / `risky edit rejected (req R): …` | already applied or reverted; re-check `get_dispatchable` if `depends_on` changed |
| `diagram approved: <name> (req R; N nodes, M edges; created …) — "note"` | the file is written. For every id under `created …` call `upsert_node` to give it a title, type and body. Then continue intake: next proposal, or `get_dispatchable` |
| `diagram rejected: <name> (req R) — "note"` | read the note, redraft, `propose_diagram(<same name>, …)` again with a rationale that answers the note. Never `write_diagram` instead |
| `needs_input (prompt P, agent-x…): "…"` | tell the owner the question and that the canvas prompt collects the answer. Do not answer for them |
| `needs_input reply (prompt P, agent-x): "v"` | `SendMessage(to=<agent-x's ref>, message="Reply to prompt P: v")` |
| `dispatch approved: node-x → agent-x …` | **Dispatch** step 4 onward |
| `dispatch rejected: node-x → agent-x …` | `set_agent_status("agent-x", "idle")`; ask the owner what to change |
| `agent-x done on node-x; notified …; now dispatchable: …` | `finalize_agent` (Dispatch 6), two-line recap via `chat_reply("root", …)`, then propose the next dispatch for each newly dispatchable node |
| `agent-x blocked on node-x: …` | tell the owner what is blocked and why; propose a fix or `ask_user`; post the same two lines with `chat_reply("root", …)` |
| `agent plan edited: agent-x — <first line>` | **Plan-edit propagation** below |
| `agent diagram edited: agent-x — <first line>` | same, but point the agent at `.whiteboard/agents/agent-x/diagrams.md` and at `write_agent_diagram` |
| `agent-x acknowledged plan edit (event N): "…"` | nothing to do; confirm to the owner in one line |

## Plan intake

1. `read_plan`. Decompose the pasted plan into units of work, one agent each. For every unit: `upsert_node(id="node-<slug>", title, type, depends_on, interfaces, body)`. `type` is the board it lives on: `hld` for services/components/subsystems, `lld` for modules/classes/files, `er` for persisted data entities.
2. Draft the diagrams and propose them — one call each, **each with a rationale in one sentence**:
   - `propose_diagram("hld", <flowchart TD over node ids>, rationale=…)` — always.
   - `propose_diagram("lld", <flowchart TD over the lld node ids>, rationale=…)` — always.
   - `propose_diagram("er", <flowchart TD over the er node ids>, rationale=…)` — **only when the plan has data entities**. If you skip it, tell the owner in one line why.
3. Wait for `diagram approved` / `diagram rejected` for each. **Never call `write_diagram`** — it bypasses the owner's approval. The owner may edit the mermaid before approving; the approved text wins, so after each approval re-read with `read_plan` and reconcile.
4. When every proposal is settled: `get_dispatchable`, report the list, and propose the first dispatch.

## Dispatch

1. `get_dispatchable`; `read_node` the node and every dependency.
2. Write the job spec (goal, read-first files, deps + their interfaces, dependents whose interfaces must stay stable, allowed code paths, deliverables, branch). Keep the server's generated sections — diagrams, progress reporting, chat, plan changes — by leaving `job_spec_md` empty when the generated spec is good enough.
3. `propose_dispatch(node_id, agent_id="agent-<slug>", job_spec_md=<spec or omitted>)`. Wait for the `dispatch approved` bullet. **Never spawn before approval.**
4. On approval: `Agent(subagent_type="whiteboard-task", prompt=<the full contents of .whiteboard/agents/agent-x/plan.md>, run in background)`. Pass the spec text, not a path.
5. Immediately: `set_agent_ref(agent_id="agent-x", claude_agent_ref=<the handle the Agent tool returned>)`. This emits the `spawned` event and is what makes chat forwarding and plan edits reach the agent. Without it you have lost the agent.
6. When the Agent tool's completion notification arrives, read its tokens and duration and call `finalize_agent(agent_id="agent-x", tokens_in=…, tokens_out=…, cost_usd=…, duration_s=…, note=<one line>)`. Then post a two-line recap with `chat_reply("root", <what landed; what is dispatchable now>)`.

### `general-purpose` fallback

If `Agent(subagent_type="whiteboard-task", …)` fails with an unknown subagent type, re-run the identical call with `subagent_type="general-purpose"` — the job spec is self-sufficient. Tell the owner once that `~/.claude/agents/whiteboard-task.md` is missing and that `make install-skill` in `~/AgentBoard` installs it. Subagents inherit only the MCP servers connected in *this* session, so if the agent reports that `mcp__whiteboard__*` is unavailable, run `/mcp` here and re-dispatch.

## Plan-edit propagation

On `agent plan edited: agent-x — <first line>` (event N, from the canvas or from disk):

1. `SendMessage(to=<agent-x's claude_agent_ref>, message="Your plan changed (event N): <first line>. Re-read .whiteboard/agents/agent-x/plan.md and call mcp__whiteboard__report_progress(agent_id=\"agent-x\", activity=<what you will do differently>, ack_event_seq=N) before continuing.")`
2. Tell the owner you forwarded it and that you are waiting for the acknowledgement.
3. If no `agent-x acknowledged plan edit (event N)` bullet arrives before the agent's next `report_progress`, say so — an unacknowledged edit means the agent is still building the old spec.
4. If the agent has no `claude_agent_ref` (never dispatched, or already `done`), do not SendMessage; tell the owner the edit only takes effect on the next dispatch.

## Working with another project

"Work with project X" / "what does project X expose" → `Agent(subagent_type="whiteboard-liaison", prompt="Peer project: <abs path>. Return its skeleton" + specifics)`. Summarise what it returns; create nodes here if the owner wants to depend on peer work. Never spawn a liaison to edit anything.

## Hard rules

- Never let subagents talk to each other; never tell one agent about another's work. If two agents must coordinate, update the node files and let the events carry it.
- `SendMessage` to a subagent is allowed for exactly four things: forwarded chat, prompt replies, plan-edit notices, diagram-edit notices. Nothing else, ever.
- Never write a diagram directly: root proposes, the owner approves. `write_diagram` is not yours to call.
- Every dispatch is recorded with `set_agent_ref` and closed with `finalize_agent`.
- Never edit `.whiteboard/PLAN.md`, `server.json`, `*.layout.json`, `~/.claude/settings.json` or another agent's folder by hand; use MCP tools and, for settings, the printed `fix_hint`.
- Every state change goes through `mcp__whiteboard__*` so the canvas and the files stay in sync.
