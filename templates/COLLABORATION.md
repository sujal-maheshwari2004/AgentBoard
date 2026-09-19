# COLLABORATION

Read this before touching anything. It is the protocol every whiteboard agent follows. Humans may edit it; agents must not.

## 1. Files are the truth

- `.whiteboard/plan/nodes/<node-id>.md` is the plan. Frontmatter (`status`, `owner`, `depends_on`, `interfaces`) is authoritative; the canvas and `PLAN.md` are projections of it.
- `.whiteboard/agents/<agent-id>/plan.md` is **your job spec**. Read it first, then `card.md` (your status and `ready_deps`), then the node file it points at.
- `.whiteboard/events.jsonl` is the only channel between agents. Nothing is "known" until it is an event.
- Never hand-edit `PLAN.md`, `server.json`, `*.layout.json`, or another agent's folder.

## 2. Status transitions

Node: `todo -> in_progress -> done`, with `blocked` reachable from `in_progress` and back.
Agent: `idle -> working -> done`, with `blocked` in between when you cannot proceed.

- On start: `mcp__whiteboard__set_agent_status(agent_id, "working")` (the server moves your node to `in_progress`).
- Blocked (missing dependency, failing prerequisite, ambiguous spec): `append_event(type="blocked", note=<what and why>)` and stop.
- Need a decision: `ask_user(agent_id, question, choices=...)`, then `get_reply(prompt_id)` (poll; it is fine to wait). Never guess on interface changes.
- Finished: `append_event(type="done", note=<what landed, test counts, deviations>)`. The server marks your node done, notifies dependents, and lists the newly dispatchable nodes for the root.

## 3. Scope boundaries

- Touch only your node's files and the code paths listed in your job spec. If the work needs a file outside that list, `ask_user` first.
- Do not change interfaces listed in `interfaces:` of your node or of nodes that depend on you. If a change is unavoidable, `ask_user` with the exact before/after signatures.
- Do not modify other nodes' frontmatter. Report what you learned in your `done`/`blocked` note instead.
- Never spawn subagents and never message another agent. Everything goes through events; the root session routes them.

## 4. Escalation

1. Re-read your `plan.md` and the node body.
2. Check `read_collaboration` and `read_node(<dependency>)` for the contract you build against.
3. `ask_user` — concise question, concrete choices when possible.
4. If no answer is needed to continue safely, proceed and record the assumption in your `done` note.

## 5. Commits

- One logical change per commit, on the branch you were given (never the default branch).
- Message: `<node-id>: <imperative summary>`; body lists interfaces added or changed.
- Run the project's tests before `done`; report counts in the event note.
