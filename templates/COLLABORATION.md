# COLLABORATION

Read this before touching anything. It is the protocol every whiteboard agent follows. Humans may edit it; agents must not.

## 1. Files are the truth

- `.whiteboard/plan/nodes/<node-id>.md` is the plan. Frontmatter (`status`, `owner`, `depends_on`, `interfaces`) is authoritative; the canvas and `PLAN.md` are projections of it.
- `.whiteboard/agents/<agent-id>/plan.md` is **your job spec**. Read it first, then `card.md` (your status, `ready_deps`, live metrics), then the node file it points at.
- `.whiteboard/agents/<agent-id>/diagrams.md` is **your** low-level design, in one `flowchart TD` fence. Box ids there are free-form (module, class, table names) — they are not node ids.
- `.whiteboard/events.jsonl` is the only channel between agents. Nothing is "known" until it is an event.
- Never hand-edit `PLAN.md`, `server.json`, `*.layout.json`, or another agent's folder.

## 2. Boards — who writes which

| board | file | contents | written by |
|---|---|---|---|
| HLD | `plan/hld.md` | services, components, subsystems (`type: hld` nodes) | root, via `propose_diagram` → owner approval |
| LLD | `plan/lld.md` | modules, classes, files (`type: lld` nodes) | root, via `propose_diagram` → owner approval |
| ER | `plan/er.md` | persisted data entities (`type: er` nodes); exists only when the plan has data | root, via `propose_diagram` → owner approval |
| your LLD | `agents/<id>/diagrams.md` | the real structure of your node: one box per module/class/table you touched | **you**, with `write_agent_diagram`, before `done` |

Root never writes a board directly and neither do you: every board change is a proposal the owner approves on the canvas, and the owner may edit the mermaid before approving. Your own `diagrams.md` needs no approval.

## 3. Status transitions

Node: `todo -> in_progress -> done`, with `blocked` reachable from `in_progress` and back.
Agent: `idle -> working -> done`, with `blocked` in between when you cannot proceed.

- On start: `set_agent_status(agent_id, "working")` (the server moves your node to `in_progress`).
- Blocked (missing dependency, failing prerequisite, ambiguous spec): `append_event(type="blocked", note=<what and why>)` and stop.
- Need a decision: `ask_user(agent_id, question, choices=...)`, then `get_reply(prompt_id)` (poll; waiting is fine). Never guess on interface changes.
- Before finishing: `write_agent_diagram(agent_id, mermaid=...)`.
- Finished: `append_event(type="done", note=<what landed, test counts, deviations>)`. The server marks your node done, notifies dependents, and lists the newly dispatchable nodes for the root.

## 4. Progress reporting

`report_progress(agent_id, activity=..., progress=..., metrics=..., files_touched=[...], final=False, ack_event_seq=None)` is cheap: it updates your card in memory and on the canvas without rewriting the plan. Call it at every milestone:

1. after reading the spec, the node and every dependency (`progress ≈ 0.1`);
2. after each file or coherent group of files lands;
3. before the test run and again after it;
4. immediately before `done` (`progress = 1.0`).

`activity` is one short present-tense line ("writing the parser fence tests"). `files_touched` is cumulative on the server and deduped — send what you touched since your last call. Do not report progress you have not made; the owner is watching the card live, and a stale heartbeat is read as a hung agent.

## 5. Metrics vocabulary

Cumulative, all optional, all merged (last value wins per key):

| key | unit | who sets it |
|---|---|---|
| `elapsed_s` | seconds since you started | you |
| `tool_calls` | count of tool calls you have made | you |
| `files_touched` | list of repo-relative paths | you (as the `files_touched` argument, not inside `metrics`) |
| `tokens_in`, `tokens_out` | token counts for the whole run | the root session, once, via `finalize_agent` |
| `cost_usd` | USD, 2–6 dp | the root session, via `finalize_agent` |

`spawned_at` is your start, `heartbeat_at` your last report, `finished_at` is set by `final=True` or by `finalize_agent`. Never write these by hand into `card.md`.

## 6. Chat threads

- Every message lives in a thread: `root` for the owner↔root conversation, `<agent-id>` for the owner↔agent conversation. There is one thread per agent and it is yours.
- When the owner chats with you, the root session forwards the text and tells you to answer. Answer **in the thread**: `chat_reply(from_id=<your agent id>, text=<answer>)`. Your final message is not a reply — nobody reads it until you exit.
- Answer chat promptly; it usually unblocks a decision. Keep answers under five lines and link to a file path rather than pasting code.
- Never post into another agent's thread, and never answer on the owner's behalf in the `root` thread.

## 7. Mid-run plan edits and acknowledgement

The owner can rewrite your `plan.md` (on the canvas or on disk) while you run. When that happens:

1. The server tells the root session, which sends you: "Your plan changed (event N): <first changed line>."
2. **Stop what you are doing.** Re-read `.whiteboard/agents/<your-id>/plan.md` in full.
3. Acknowledge before continuing: `report_progress(agent_id, activity=<what you will do differently>, ack_event_seq=N)`. That is what the owner sees as "acknowledged plan edit (event N)".
4. If the new plan invalidates work already done, say so in the acknowledgement's `activity` and, if it needs a decision, `ask_user`.
5. If you deviate from the plan on your own initiative, write the deviation into the spec first with `write_agent_plan(agent_id, plan_md=...)`. The spec on disk must always describe what you are actually doing.

## 8. Scope boundaries

- Touch only your node's files and the code paths listed in your job spec. If the work needs a file outside that list, `ask_user` first.
- Do not change interfaces listed in `interfaces:` of your node or of nodes that depend on you. If a change is unavoidable, `ask_user` with the exact before/after signatures.
- Do not modify other nodes' frontmatter. Report what you learned in your `done`/`blocked` note instead.
- Never spawn subagents and never message another agent. Everything goes through events; the root session routes them.

## 9. Escalation

1. Re-read your `plan.md` and the node body.
2. Check `read_collaboration` and `read_node(<dependency>)` for the contract you build against.
3. `ask_user` — concise question, concrete choices when possible. Keep reporting progress while you wait.
4. Still stuck after an answer, or the answer changes scope: `append_event(type="blocked", ...)` and stop. Do not improvise around a blocked dependency.
5. If no answer is needed to continue safely, proceed and record the assumption in your `done` note.

## 10. Commits

- One logical change per commit, on the branch you were given (never the default branch).
- Message: `<node-id>: <imperative summary>`; body lists interfaces added or changed.
- Run the project's tests before `done`; report counts in the event note.
