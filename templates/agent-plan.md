# Job spec: node-example

## Goal

**Human-readable title** (`node-example`, type `hld`)

One paragraph: what "done" means for this node.

## Your node file path

`.whiteboard/plan/nodes/node-example.md`

Update its body with design notes and progress; the server keeps the frontmatter in sync.

## Status protocol

Use the `whiteboard` MCP server, tool names exactly as written:

If the `mcp__whiteboard__*` tools are not available in your session, stop immediately and say which
tool is missing in your final message: without them nothing you do is recorded and the root session
cannot see you.

1. First thing: `mcp__whiteboard__set_agent_status(agent_id="agent-example", status="working")`.
2. Questions for the human: `mcp__whiteboard__ask_user(agent_id="agent-example", question=..., node_id="node-example")`,
   then poll `mcp__whiteboard__get_reply(prompt_id=...)`.
3. Last thing: `mcp__whiteboard__append_event(agent_id="agent-example", node_id="node-example", type="done", note=<what landed + test counts>)`
   — or `type="blocked"` / `type="needs_input"` with a note explaining why.

`done` marks the node done and notifies the agents that depend on it; do not call it before your
tests pass.

## Interfaces you must provide

- (signatures other nodes may rely on)

## Interfaces you consume

- (node ids and the interfaces you build against)

## Dependents waiting on you

- (node ids whose interfaces must stay stable)

## Progress reporting

`mcp__whiteboard__report_progress(agent_id="agent-example", activity=<one short present-tense line>, progress=<0.0-1.0>, metrics={"tool_calls": <n>, "elapsed_s": <n>}, files_touched=[<paths changed since your last report>])`

Call it at every milestone: after reading the spec, the node and every dependency
(`progress ≈ 0.1`); after each file or coherent group of files lands; before and after the test
run; immediately before `done` (`progress = 1.0`).

`files_touched` is deduped and accumulated server-side. `tokens_in`, `tokens_out` and `cost_usd`
are filled in by the root session — do not send them.

The human watches this live. Never report progress you have not made.

## Chat

Owner chat reaches you as a message telling you to answer. Answer in your own thread:
`mcp__whiteboard__chat_reply(from_id="agent-example", text=<your answer>)`.

Your final message is not a reply — it is not read until you exit. Never post in another agent's
thread and never answer on the owner's behalf in the `root` thread.

## If your plan changes

If you must deviate from this spec, rewrite it first:
`mcp__whiteboard__write_agent_plan(agent_id="agent-example", plan_md=<updated spec>)`. The spec on
disk must always describe what you are actually doing.

If the owner edits your plan mid-run you are told the event number N. Stop what you are doing,
re-read `.whiteboard/agents/agent-example/plan.md` in full, then acknowledge with
`mcp__whiteboard__report_progress(agent_id="agent-example", activity=<what changes>, ack_event_seq=N)`
**before** doing anything else.

## Scope

- Files/paths you may edit:
- Out of scope:
- Never edit other nodes' files under `.whiteboard/plan/nodes/`; report needed changes via
  `append_event` instead.
- Do not message other agents about each other's work; everything goes through events.

## Read first

- `.whiteboard/COLLABORATION.md`
- `.whiteboard/PLAN.md`
- `.whiteboard/plan/nodes/node-example.md`

## Deliverables

- Code, tests, and a `done` event whose note lists what landed and any deviations.
