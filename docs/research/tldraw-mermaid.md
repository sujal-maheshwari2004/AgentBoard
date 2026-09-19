# Research: tldraw SDK, Mermaid flowchart subset, frontend stack

Verified 2026-09-20 against tldraw.dev, mermaid.js.org, npm registry.

## Versions

| Package | Version | Notes |
|---|---|---|
| `tldraw` | 5.4.2 | peer `react ^18.2.0 || ^19.2.1` (React 19 floor is 19.2.1). Source-available license; localhost/plain-HTTP is "development mode": no key, no watermark. |
| `react`, `react-dom` | 19.3.0 | |
| `vite` | 8.3.0 | needs Node 20.19+ / 22.12+ |
| `@dagrejs/dagre` | 3.1.1 | maintained; the unscoped `dagre` (0.8.5) is stale. MIT. |
| `mermaid` (JS) | 12.0.0 | NOT used client-side |
| `@mermaid-js/parser` | 2.0.0 | does NOT cover flowcharts (still Jison) |
| `@tldraw/mermaid` | 5.4.2 | text→shapes only, invents own ids, no write-back: NOT used |

Install: `pnpm add tldraw @dagrejs/dagre`. Import `tldraw/tldraw.css`. The `<Tldraw>` container must have explicit size.

## tldraw essentials (v5 API; many blog snippets are v2–v4 and wrong)

### Stable ids
`createShapeId('n_auth')` → `'shape:n_auth'` (pure prefix). Namespaces: `n_<nodeId>` plan nodes, `a_<agentId>` agent cards, `f_<name>` frames, `e_<from>__<to>` arrows. Bindings: `createBindingId('b_<edge>_start')` (verify it accepts a seed; else look bindings up with `getBindingsFromShape`). Duplicate ids throw on create. Also stamp `meta: { kind: 'plan-node' | 'agent-card' | 'plan-frame' | 'edge', planId }` on every shape we create; `meta` is free-form and survives snapshots.

### Text is `richText`
`props: { richText: toRichText('Label') }`. `props.text` fails validation since 4.0. Read back with `renderPlaintextFromRichText(editor, richText)`. Never diff richText JSON; compare plaintext. Formatting (bold etc.) is lossy on write-back by design.

### geo shapes
`type: 'geo', props: { geo, w, h, color, fill, dash, size, font, align, verticalAlign, labelColor, richText, url, growY, scale }`. `geo` ∈ rectangle | ellipse | oval | diamond | hexagon | triangle | pentagon | octagon | star | rhombus | trapezoid | cloud | x-box | check-box | heart | arrow-*.
Mermaid → geo: `[ ]` rectangle, `( )`/`([ ])` oval, `{ }` diamond, `{{ }}` hexagon, `(( ))` ellipse, `[[ ]]`/`[( )]` rectangle.

### Arrows and bindings
```ts
editor.run(() => {
  editor.createShape({ id: arrowId, type: 'arrow', x, y,
    props: { start: {x:0,y:0}, end: {x:dx,y:dy}, kind: 'elbow', arrowheadStart: 'none', arrowheadEnd: 'arrow', richText: toRichText(label) } })
  editor.createBindings([
    { id: createBindingId(`b_${key}_start`), fromId: arrowId, toId: fromShapeId, type: 'arrow', props: { terminal: 'start', normalizedAnchor: {x:.5,y:.5}, isExact: false, isPrecise: false } },
    { id: createBindingId(`b_${key}_end`),   fromId: arrowId, toId: toShapeId,   type: 'arrow', props: { terminal: 'end',   normalizedAnchor: {x:.5,y:.5}, isExact: false, isPrecise: false } },
  ])
}, { history: 'ignore' })
```
Bindings are separate records (`typeName: 'binding'`, `type: 'arrow'`, `fromId` = arrow, `toId` = target). Deleting a shape auto-removes bindings. **Arrow reconnection is a binding remove+add, not a shape change.** Resolve topology at end of batch:
```ts
const bs = editor.getBindingsFromShape(arrowId, 'arrow') as TLArrowBinding[]
const from = bs.find(b => b.props.terminal === 'start')?.toId ?? null
const to   = bs.find(b => b.props.terminal === 'end')?.toId ?? null   // null ⇒ dangling, don't persist
```
Custom shapes need `canBind() { return true }` to accept arrows.

### Frames and collapse
`type: 'frame', props: { w, h, name, color }`; children set `parentId: frameId` and use **frame-local coordinates**. Convert with `editor.getShapePageTransform(id)` / `getShapeParentTransform`. Children via `editor.getSortedChildIdsForParent(frameId)`. No native collapse: use the `getShapeVisibility` prop (replaces `isShapeHidden`):
```tsx
<Tldraw getShapeVisibility={(s) => (s.meta.hidden ? 'hidden' : 'inherit')} />
```
Keep the callback pure (reads only `shape`). Hidden shapes stay in the store (no deletions seen by the diff). Collapse = set `meta.hidden` on children + shrink frame height, remember `expandedH` in frame meta. Folder icon in the `OnTheCanvas` component slot (pans/zooms with the page).

### Custom shape (agent card)
```tsx
declare module 'tldraw' { interface TLGlobalShapePropsMap { 'agent-card': { w:number; h:number; agentId:string; name:string; status:'idle'|'working'|'blocked'|'done'; detail:string; ready:boolean } } }
class AgentCardUtil extends ShapeUtil<TLShape<'agent-card'>> {
  static override type = 'agent-card'
  static override props: RecordProps<...> = { w: T.number, h: T.number, agentId: T.string, name: T.string, status: T.literalEnum('idle','working','blocked','done'), detail: T.string, ready: T.boolean }
  getDefaultProps() {...}
  override canEdit() { return false }  override canResize() { return true }  override canBind() { return true }
  getGeometry(s) { return new Rectangle2d({ width: s.props.w, height: s.props.h, isFilled: true }) }
  override onResize(s, info) { return resizeBox(s, info) }
  component(s) { return <HTMLContainer style={{ pointerEvents: 'all', ... }}> ... <button onPointerDown={e => e.stopPropagation()} onClick={...}/> </HTMLContainer> }
  getIndicatorPath(s) { const p = new Path2D(); p.rect(0,0,s.props.w,s.props.h); return p }   // v5: indicator() JSX is gone
}
<Tldraw shapeUtils={[AgentCardUtil]} />
```
`pointerEvents:'all'` on the container and `stopPropagation` on pointer-down of buttons are required. Verify `T.literalEnum` exists in 5.4 (alt: `T.setEnum`).

### Change listening and echo suppression
```ts
// user edits only, document scope (shapes + bindings, no camera/selection)
const off = editor.store.listen(change => collect(change.changes /* added, updated: [prev,next], removed */), { source: 'user', scope: 'document' })
// server-originated writes: tagged 'remote' so the listener above does not fire, and no undo pollution
editor.store.mergeRemoteChanges(() => editor.run(() => { editor.updateShapes(...); editor.deleteShapes(...) }, { history: 'ignore' }))
```
`editor.sideEffects.registerOperationCompleteHandler` fires once per transaction (good place for the debounced flush). `registerBeforeDeleteHandler('shape', (shape, source) => source==='user' && shape.meta.kind==='plan-node' ? false : undefined)` vetoes plan-node deletion; send a `deleted` op instead and let the server decide.
Detect: moved (x/y/parentId), resized (props.w/h), renamed (plaintext differs), created, deleted, arrow terminal set/cleared (binding records).

### Snapshot/update/batch
`getSnapshot(editor.store)` / `loadSnapshot(editor.store, snap)` are standalone functions (not store methods). Prefer targeted `createShapes/updateShapes/deleteShapes` (each partial needs `id` and `type`) inside `editor.run(fn, { history: 'ignore' })` (`editor.batch` is the old name). On full sync: delete only shapes we own (`isOurs(id)`), never the human's freehand shapes.

### UI
`<Tldraw hideUi components={{ ContextMenu: null, OnTheCanvas: FrameChrome }} />` (`hideUi` does not hide the context menu). Do not null `Toasts`/`Dialogs`/`A11y` (disables features). Chat box, paste-plan box, needs-input modal, dispatch pop-up, risky-edit confirm: React siblings outside `<Tldraw>`, driven by React state (never store records). `InFrontOfTheCanvas` + `editor.pageToViewport()` for anchoring a pop-up near a shape. Camera: `zoomToFit()`, `zoomToSelection()`, `select(id)`, `setCamera`, options via `options={{ camera: {...} }}` (v5 moved `cameraOptions` into `options`).

### Gotchas
- No `persistenceKey` (IndexedDB would shadow files). Many official examples include it.
- StrictMode double `onMount`: return cleanup from `onMount`, make seeding idempotent (`getShape(id) ? update : create`), guard the socket with a `closedRef`.
- Assets (fonts/icons) load from tldraw CDN by default; self-host via `@tldraw/assets/urls` if offline matters.
- v5 renames: `inferDarkMode`→`colorScheme`, `useIsDarkMode`→`useColorMode`; overlay slots (`Brush`, `Handle`, ...) removed; `editor.toImage()` for export.
- Package is `tldraw`, not `@tldraw/tldraw`.
- tldraw publishes https://tldraw.dev/llms-full.txt for reference.

## Auto-layout: dagre
```ts
import dagre from '@dagrejs/dagre'
const g = new dagre.graphlib.Graph({ multigraph: true })
g.setGraph({ rankdir: dir /* TD/TB→'TB', LR, RL, BT */, nodesep: 60, ranksep: 90, marginx: 40, marginy: 40 })
g.setDefaultEdgeLabel(() => ({}))
nodes.forEach(n => g.setNode(n.id, { width: n.w, height: n.h }))
edges.forEach(e => g.setEdge(e.from, e.to, {}, `${e.from}->${e.to}`))
dagre.layout(g)
// dagre gives CENTRES; tldraw wants TOP-LEFT
pos[id] = { x: g.node(id).x - w/2, y: g.node(id).y - h/2 }
```
Run only for nodes without a saved position, or on explicit "Re-layout" (pinned nodes never move). Never relayout on every server push.

## Layout persistence: sidecar `<diagram>.layout.json`
Next to each diagram (`plan/hld.layout.json`), keyed by stable ids, page coordinates, gitignored by default:
```json
{ "version": 1, "direction": "TD", "updatedAt": "...",
  "frames": { "plan-board": { "x":0, "y":0, "w":1200, "h":800, "collapsed": false } },
  "nodes": { "node-parse": { "x":120, "y":340, "w":200, "h":80, "parent":"plan-board", "pinned": true } },
  "edges": { "node-a__node-b": { "startAnchor":[0.5,1], "endAnchor":[0.5,0], "precise": true } } }
```
Rationale: markdown stays semantic; dragging a box is a zero-line diff on the plan; the watcher must ignore `*.layout.json`; write debounced (~500 ms) and atomically; keep unknown ids for one cycle then GC.

## Mermaid flowchart subset (official syntax: https://mermaid.js.org/syntax/flowchart.html)

Header: `flowchart TD|TB|BT|LR|RL` or `graph <DIR>` (`TD`≡`TB`).
Node shapes: `id`, `id[Label]`, `id(Label)`, `id([Label])`, `id[[Label]]`, `id[(Label)]`, `id((Label))`, `id(((Label)))`, `id>Label]`, `id{Label}`, `id{{Label}}`, `id[/L/]`, `id[\L\]`, `id[/L\]`, `id[\L/]`. Quoted labels `id["Text (x)"]`. Class shorthand `id:::cls` attaches to the node token (strip and stash). Ignore `A@{ shape: ... }` forms (passthrough).
Links: `-->`, `---`, `-.->`, `-.-`, `==>`, `===`, `--o`, `--x`, `<-->`; extra dashes lengthen the rank gap (normalize to canonical on emit). Labels: `A -->|text| B` and `A -- text --> B` (also `-. text .->`, `== text ==>`). Chains `A --> B --> C`; groups `A & B --> C & D` (cross product).
Subgraphs: `subgraph id [Title]` ... `end`, optional `direction LR` inside.
Comments: `%% ...` on its own line. Ignore as passthrough: `style`, `classDef`, `class`, `linkStyle`, `click`, YAML frontmatter config.

### Parser design (Python, no deps)
Line-oriented: comment → passthrough; directive → kind/direction; ignored keyword lines → passthrough; `subgraph`/`end` → context; else split the line on link operators into alternating node-groups and links, register nodes, emit the cross product of edges. **Alternation order is load-bearing**: `(((` → `((` → `([` → `[(` → `[[` → `{{` → `[/…\]`/`[\…/]`/`[/…/]`/`[\…\]` → `>…]` → `[` → `(` → `{`. `register_node` upgrades (fills label/shape) but never downgrades a later bare reference. Strip surrounding quotes and remember `quoted`. Node id regex `[A-Za-z0-9_][A-Za-z0-9_.-]*`; the server additionally requires plan node ids to match `^[A-Za-z0-9_-]+$`.

AST: `Node(id, label|None, shape, classes, quoted, order)`, `Edge(src, dst, label|None, style, head, length, label_form, order)`, `Subgraph(id, title, direction, members, order)`, `Passthrough(text, order)`, `MermaidDoc(kind, direction, nodes: dict (insertion-ordered), edges, subgraphs, passthrough, indent)`.

Serializer determinism: nodes in first-seen order, edges in declaration order, new nodes append at the end, deleted nodes removed in place, canonical `-->` and `|label|`, 4-space indent, passthrough lines verbatim at their original relative position (attach each to the statement index it followed), auto-quote labels matching `[\[\](){}|"<>&#]|^\s|\s$`, write only if bytes differ.
Tests: idempotence `serialize(parse(x)) == serialize(parse(serialize(parse(x))))` and semantic round-trip `parse(serialize(parse(x))) == parse(x)` over a fixture corpus.

Also needed: `extract_mermaid_blocks(markdown) -> list[(start_line, end_line, text)]` for ```mermaid fences and `replace_block(markdown, index, new_text)`.

## Frontend stack
- Scaffold: Vite + React + TS (manual files are fine), `pnpm build` → `canvas/dist`, committed so the server can serve it without Node at runtime.
- Serve from FastAPI: explicit `GET /` → `dist/index.html`, `/assets` → `StaticFiles(dist/assets)`, SPA fallback route registered after the MCP mount (never `StaticFiles` at `/`; it answers 405 to `POST /mcp`). Vite `base` stays `/`.
- Dev: `vite.config.ts` `server.proxy = { '/ws': { target: 'ws://127.0.0.1:<port>', ws: true }, '/api': { target: 'http://127.0.0.1:<port>' } }`. Client always uses `${location.protocol==='https:'?'wss:':'ws:'}//${location.host}/ws` so dev and prod share code. Same origin → no CORS. Server checks the `Origin` header starts with `http://localhost:` or `http://127.0.0.1:`.
- WS client: reconnect with full-jitter exponential backoff capped at 10 s, outbound queue flushed on open, `closedRef` to survive StrictMode, `client.hello{lastSeq}` on open, ignore stale `seq`.
- Server → client and client → server message catalogue: see `docs/CONTRACTS.md` (authoritative).
