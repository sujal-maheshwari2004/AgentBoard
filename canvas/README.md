# canvas — the tldraw whiteboard client

React + tldraw 5.4 single-page app. It renders the plan (`.whiteboard/`) that the Python server
streams over a websocket, sends human edits back as semantic ops, and hosts the chat / paste-plan /
needs-input / dispatch / risky-edit panels. Files on disk are the only truth: the canvas keeps no
local persistence (no `persistenceKey`, no IndexedDB).

Protocol and file formats: `../docs/CONTRACTS.md` (§1 layout sidecar, §4 ops, §6 websocket).

## Layout of `src/`

| path | what |
|---|---|
| `ws/client.ts` | envelope `{type, payload, seq, ts, replyTo}`, reconnect with full-jitter backoff (cap 10 s), outbound queue flushed on open, `client.hello {clientId, lastSeq, protocol: 1}` on every open, stale `event.append` replays dropped |
| `state/types.ts`, `state/store.ts` | CONTRACTS models; small plain store (`useSyncExternalStore`) with a reducer for every server message type, prompt queues, 200-entry event ticker, socket/bridge status, `selectedAgentId` |
| `shapes/ids.ts` | stable shape ids (`shape:n_<node>`, `shape:a_<agent>`, `shape:f_plan-board`, `shape:e_<src>__<dst>`, `binding:b_<src>__<dst>_<terminal>`) and `parseShapeId` |
| `shapes/AgentCardUtil.tsx` | custom `agent-card` shape (status dot, READY badge, assigned node, Chat / Focus node buttons) |
| `layout/dagre.ts` | dagre auto-layout, centre → top-left conversion, "only place what has no position", pinned nodes never move |
| `sync/apply.ts` | server truth → shapes; every write is `store.mergeRemoteChanges(() => editor.run(fn, {history: 'ignore'}))`; idempotent upserts; only deletes shapes whose `meta.kind` is ours |
| `sync/collect.ts` | user edits → `canvas.edit` ops (`renamed`, `node-created`, `deleted`, `edge-created`, `edge-deleted`, `edge-rerouted`) debounced 250 ms, and `canvas.layout` patches debounced 500 ms; `deriveOps` is pure and unit-tested |
| `sync/frame.tsx` | plan frame chrome (collapse/expand folder button in the `OnTheCanvas` slot), `getShapeVisibility` |
| `sync/wire.ts` | glue: socket ⇄ store ⇄ editor, mounted from `<Tldraw onMount>` and torn down by its cleanup (StrictMode-safe) |
| `panels/*` | screen-space React panels beside `<Tldraw>`: `Toolbar`, `EventTicker`, `ChatBox`, `PastePlan`, `NeedsInputModal`, `DispatchPopup`, `RiskyEditConfirm` |
| `app/App.tsx` | `<Tldraw hideUi components={{ContextMenu: null, OnTheCanvas: FrameChrome}} shapeUtils={[AgentCardUtil]} getShapeVisibility onMount>` + panels |

## Develop against the mock server

```sh
cd canvas
pnpm install
pnpm mock                      # ws + static server on 127.0.0.1:43999 (MOCK_PORT to change)
WHITEBOARD_PORT=43999 pnpm dev # vite on :5173, proxies /ws, /api, /skeleton to the mock
```

Open http://localhost:5173. The mock (`mock-server.mjs`) replays
`tests/fixtures/protocol/snapshot.json` on `client.hello`, then the `{delayMs, message}` list in
`tests/fixtures/protocol/scripted.json` (a node status change, an agent card going `working`, a
`needs_input`, a `dispatch.request`). It logs every client message to stdout and answers like the
real server: `edit.ack` for cosmetic ops, `risky_edit.request` for edge/delete ops (approve → applies
and re-broadcasts; reject → `edit.reject`), `layout.update` echo for `canvas.layout`, cleared
positions for `plan.relayout`, a canned `reply` event for `chat.message`.

Against the real Python server, set `WHITEBOARD_PORT` to the port in `<project>/.whiteboard/server.json`.

## Build

```sh
pnpm build   # tsc -b (strict, includes tests) && vite build → dist/
pnpm test    # vitest, jsdom
```

`dist/` is committed (force-included by `.gitignore`) so the server can serve it without Node.
Serve `dist/index.html` at `/` and `dist/assets` at `/assets` (see `docs/research/tldraw-mermaid.md`,
"Frontend stack"). The client always connects to `ws(s)://<same host>/ws`.

To eyeball the built bundle without vite: `pnpm build && pnpm mock` then open http://127.0.0.1:43999.

## How the canvas maps the plan

- One frame `Plan board` (`shape:f_plan-board`) holds every plan node as a `geo` rectangle whose label
  is the node title. Colours: todo grey outline (dashed when all deps are done), in_progress blue,
  blocked red, done green, all with `fill: 'semi'`.
- Edges are `arrow` shapes (`kind: 'elbow'`) with two centre-anchored `isPrecise: false` bindings.
  `A --> B` means B depends on A.
- Agent cards sit to the right of the frame.
- Positions come from the layout sidecar (`layout[diagram].nodes[id]`, page coordinates); nodes
  without one are placed by dagre once. Dragging a node sends `canvas.layout` with `pinned: true`.
  "Re-layout" sends `plan.relayout` and re-runs dagre locally for unpinned nodes.
- Drawing a rectangle inside the frame and typing a label creates a node with the provisional id
  `node-<slug-of-label>`; drawing an arrow between two nodes creates an edge once both ends bind.
- Deleting a plan node is vetoed locally and sent as a `deleted` op; the server decides (a
  `risky_edit.request` pops up). Rejected edits put the canvas back to the last server truth.
- Right-click is disabled; set a node's status with the dropdown in the toolbar (→ `node.status`).
