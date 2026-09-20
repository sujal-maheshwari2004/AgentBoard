// Server truth → tldraw store. Every write is tagged remote (so the user-edit listener stays
// quiet) and history-ignored (no undo pollution). Every function is idempotent.
import {
  react,
  toRichText,
  type Editor,
  type TLArrowBinding,
  type TLArrowShape,
  type TLDefaultColorStyle,
  type TLDefaultDashStyle,
  type TLDefaultFillStyle,
  type TLFrameShape,
  type TLGeoShape,
  type TLShape,
  type TLShapeId,
  type TLShapePartial,
} from 'tldraw'
import { AGENT_CARD_H, AGENT_CARD_W, type AgentCardShape } from '../shapes/AgentCardUtil'
import { PLAN_NODE_H, PLAN_NODE_W, type PlanNodeShape } from '../shapes/PlanNodeUtil'
import { borderAlpha, zoomAtom } from './zoom'
import {
  PLAN_FRAME_NAME,
  PLAN_FRAME_TITLE,
  agentShapeId,
  bindingId,
  edgeShapeId,
  frameShapeId,
  nodeShapeId,
  parseEdgeKey,
  shapeMeta,
  type ShapeKind,
} from '../shapes/ids'
import { FRAME_PAD, NODE_H, NODE_W, boundsOf, positionsForMissing, relayoutUnpinned, type ExistingPosition, type LayoutEdgeInput, type LayoutNodeInput } from '../layout/dagre'
import type { AgentCard, Layout, LayoutFrame, NodeStatus, PlanEdge, PlanNode, PlanSnapshot } from '../state/types'
import { edgeKey, nodeReady, primaryDiagram } from '../state/types'

export const DEFAULT_FRAME: LayoutFrame = { x: 0, y: 0, w: 1200, h: 800, collapsed: false }
export const COLLAPSED_H = 48
export const AGENT_GAP = 60
/** the plan-node label hangs below the box (B.3); reserve room for it when growing the frame */
export const PLAN_NODE_LABEL_H = 40

export function remote(editor: Editor, fn: () => void): void {
  editor.store.mergeRemoteChanges(() => editor.run(fn, { history: 'ignore' }))
}

export function kindOf(shape: TLShape | undefined): ShapeKind | null {
  const k = shape?.meta?.kind
  return k === 'plan-node' || k === 'agent-card' || k === 'plan-frame' || k === 'edge' ? k : null
}

export function isOurs(shape: TLShape | undefined): boolean {
  return kindOf(shape) !== null
}

export interface NodeStyle {
  color: TLDefaultColorStyle
  fill: TLDefaultFillStyle
  dash: TLDefaultDashStyle
}

export function nodeStyle(status: NodeStatus, ready: boolean): NodeStyle {
  switch (status) {
    case 'in_progress':
      return { color: 'blue', fill: 'semi', dash: 'solid' }
    case 'blocked':
      return { color: 'red', fill: 'semi', dash: 'solid' }
    case 'done':
      return { color: 'green', fill: 'semi', dash: 'solid' }
    default:
      return { color: 'grey', fill: 'none', dash: ready ? 'dashed' : 'solid' }
  }
}

// ---------- lookups (adopt human-drawn shapes that were stamped with our meta) ----------

function findByMeta(editor: Editor, kind: ShapeKind, planId: string): TLShape | undefined {
  for (const s of editor.getCurrentPageShapes()) {
    if (s.meta?.kind === kind && s.meta?.planId === planId) return s
  }
  return undefined
}

/**
 * Either our `plan-node` shape or a human-drawn `geo` box we adopted (stamped `meta.kind`).
 * Both carry numeric w/h, which is all the layout code needs.
 */
export type NodeShape = TLShape & { props: { w: number; h: number } }

export function findNodeShape(editor: Editor, id: string): NodeShape | undefined {
  const direct = editor.getShape(nodeShapeId(id))
  if (direct) return direct as NodeShape
  return findByMeta(editor, 'plan-node', id) as NodeShape | undefined
}

export function findEdgeShape(editor: Editor, src: string, dst: string): TLArrowShape | undefined {
  const direct = editor.getShape<TLArrowShape>(edgeShapeId(src, dst))
  if (direct) return direct
  return findByMeta(editor, 'edge', edgeKey(src, dst)) as TLArrowShape | undefined
}

export function findAgentShape(editor: Editor, id: string): AgentCardShape | undefined {
  return editor.getShape<AgentCardShape>(agentShapeId(id))
}

export function getFrame(editor: Editor): TLFrameShape | undefined {
  return editor.getShape<TLFrameShape>(frameShapeId(PLAN_FRAME_NAME))
}

// ---------- frame ----------

export function ensureFrame(editor: Editor, layoutFrame?: LayoutFrame): TLFrameShape {
  const id = frameShapeId(PLAN_FRAME_NAME)
  const existing = editor.getShape<TLFrameShape>(id)
  if (existing) return existing
  const f = { ...DEFAULT_FRAME, ...(layoutFrame ?? {}) }
  const collapsed = !!f.collapsed
  remote(editor, () => {
    editor.createShape<TLFrameShape>({
      id,
      type: 'frame',
      x: f.x,
      y: f.y,
      props: { w: f.w, h: collapsed ? COLLAPSED_H : f.h, name: PLAN_FRAME_TITLE, color: 'black' },
      meta: shapeMeta('plan-frame', PLAN_FRAME_NAME, { collapsed, expandedH: f.h }),
    })
  })
  return editor.getShape<TLFrameShape>(id)!
}

function frameOrigin(editor: Editor): { x: number; y: number } {
  const f = getFrame(editor)
  return f ? { x: f.x, y: f.y } : { x: DEFAULT_FRAME.x, y: DEFAULT_FRAME.y }
}

/** grow (never shrink) the frame so every child fits, unless collapsed */
export function fitFrame(editor: Editor): void {
  const frame = getFrame(editor)
  if (!frame || frame.meta.collapsed) return
  let w = frame.props.w
  let h = frame.props.h
  for (const cid of editor.getSortedChildIdsForParent(frame.id)) {
    const s = editor.getShape(cid)
    if (!s || (s.type !== 'geo' && s.type !== 'plan-node')) continue
    const g = s as NodeShape
    w = Math.max(w, g.x + g.props.w + FRAME_PAD)
    // the label hangs below the box, so leave room for it inside the frame
    h = Math.max(h, g.y + g.props.h + PLAN_NODE_LABEL_H + FRAME_PAD)
  }
  if (w !== frame.props.w || h !== frame.props.h) {
    remote(editor, () => {
      editor.updateShape<TLFrameShape>({ id: frame.id, type: 'frame', props: { w, h }, meta: { ...frame.meta, expandedH: h } })
    })
  }
}

// ---------- nodes ----------

export interface NodeContext {
  byId: Map<string, PlanNode>
}

export function nodeContext(nodes: Iterable<PlanNode>): NodeContext {
  return { byId: new Map(Array.from(nodes, (n) => [n.id, n])) }
}

/** `pos` is frame-local (top-left). Only used on create; existing shapes never move here. */
export function upsertNode(editor: Editor, node: PlanNode, ctx: NodeContext, pos?: { x: number; y: number; w?: number; h?: number }): void {
  const frame = ensureFrame(editor)
  const ready = nodeReady(node, ctx.byId)
  const dispatched = node.owner != null
  const existing = findNodeShape(editor, node.id)
  const props = {
    nodeId: node.id,
    title: node.title,
    // the node id, or the owning agent once dispatched (B.3)
    subtitle: dispatched ? String(node.owner) : node.id,
    type: node.type,
    status: node.status,
    ready,
    dispatched,
    owner: node.owner ?? '',
  }
  remote(editor, () => {
    if (existing) {
      const meta = { ...existing.meta, kind: 'plan-node', planId: node.id, status: node.status, provisional: false }
      if (existing.type === 'plan-node') {
        editor.updateShape<PlanNodeShape>({ id: existing.id, type: 'plan-node', props, meta })
      } else {
        // a box the human drew and the server adopted: keep it a `geo`, restyle it the v1 way
        editor.updateShape<TLGeoShape>({
          id: existing.id,
          type: 'geo',
          props: { ...nodeStyle(node.status, ready), richText: toRichText(node.title) },
          meta,
        })
      }
      return
    }
    const p = pos ?? { x: FRAME_PAD, y: FRAME_PAD }
    editor.createShape<PlanNodeShape>({
      id: nodeShapeId(node.id),
      type: 'plan-node',
      parentId: frame.id,
      x: p.x,
      y: p.y,
      props: { ...props, w: p.w ?? PLAN_NODE_W, h: p.h ?? PLAN_NODE_H },
      meta: shapeMeta('plan-node', node.id, { status: node.status, hidden: !!frame.meta.collapsed }),
    })
  })
}

export function deleteNode(editor: Editor, id: string): void {
  const shape = findNodeShape(editor, id)
  const ids: TLShapeId[] = []
  if (shape && kindOf(shape) === 'plan-node') ids.push(shape.id)
  for (const s of editor.getCurrentPageShapes()) {
    if (kindOf(s) !== 'edge') continue
    const pair = parseEdgeKey(String(s.meta.planId))
    if (pair && (pair.src === id || pair.dst === id)) ids.push(s.id)
  }
  if (ids.length) remote(editor, () => editor.deleteShapes(ids))
}

// ---------- edges ----------

function centerOf(shape: TLGeoShape | TLShape): { x: number; y: number } {
  const w = 'w' in shape.props ? (shape.props as { w: number }).w : 0
  const h = 'h' in shape.props ? (shape.props as { h: number }).h : 0
  return { x: shape.x + w / 2, y: shape.y + h / 2 }
}

function ensureBindings(editor: Editor, arrowId: TLShapeId, src: string, dst: string, fromId: TLShapeId, toId: TLShapeId): void {
  const bindings = editor.getBindingsFromShape<TLArrowBinding>(arrowId, 'arrow')
  const start = bindings.find((b) => b.props.terminal === 'start')
  const end = bindings.find((b) => b.props.terminal === 'end')
  const stale = bindings.filter((b) => (b.props.terminal === 'start' && b.toId !== fromId) || (b.props.terminal === 'end' && b.toId !== toId))
  if (stale.length) editor.deleteBindings(stale)
  const props = { normalizedAnchor: { x: 0.5, y: 0.5 }, isExact: false, isPrecise: false, snap: 'none' as const }
  if (!start || start.toId !== fromId) {
    editor.createBindings<TLArrowBinding>([{ id: bindingId(src, dst, 'start'), type: 'arrow', fromId: arrowId, toId: fromId, props: { ...props, terminal: 'start' } }])
  }
  if (!end || end.toId !== toId) {
    editor.createBindings<TLArrowBinding>([{ id: bindingId(src, dst, 'end'), type: 'arrow', fromId: arrowId, toId, props: { ...props, terminal: 'end' } }])
  }
}

/**
 * B.4: solid = settled, marching dashes = in flight. An edge into a blocked node wins, so the
 * failing path is lit up the spine (Phoenix). The renderer reads this off `meta` via the
 * ShapeWrapper, which turns it into `data-wb-edge` for `styles/edges.css`.
 */
export type EdgeState = 'default' | 'working' | 'done' | 'blocked'

export function edgeState(srcStatus: string, dstStatus: string): EdgeState {
  if (dstStatus === 'blocked') return 'blocked'
  if (srcStatus === 'in_progress') return 'working'
  if (srcStatus === 'done') return 'done'
  return 'default'
}

function statusOf(shape: NodeShape | undefined): string {
  return String(shape?.meta?.status ?? 'todo')
}

/** re-stamp every edge whose endpoints' statuses moved (cheap: only changed metas are written) */
export function refreshEdgeStates(editor: Editor): void {
  const patches: TLShapePartial[] = []
  for (const s of editor.getCurrentPageShapes()) {
    if (kindOf(s) !== 'edge') continue
    const pair = parseEdgeKey(String(s.meta.planId))
    if (!pair) continue
    const srcStatus = statusOf(findNodeShape(editor, pair.src))
    const dstStatus = statusOf(findNodeShape(editor, pair.dst))
    if (s.meta.srcStatus === srcStatus && s.meta.dstStatus === dstStatus) continue
    patches.push({ id: s.id, type: s.type, meta: { ...s.meta, srcStatus, dstStatus } } as TLShapePartial)
  }
  if (patches.length) remote(editor, () => editor.updateShapes(patches))
}

export function upsertEdge(editor: Editor, edge: PlanEdge): void {
  const from = findNodeShape(editor, edge.src)
  const to = findNodeShape(editor, edge.dst)
  if (!from || !to) return
  const frame = ensureFrame(editor)
  const existing = findEdgeShape(editor, edge.src, edge.dst)
  const key = edgeKey(edge.src, edge.dst)
  const label = toRichText(edge.label ?? '')
  const srcStatus = statusOf(from)
  const dstStatus = statusOf(to)
  remote(editor, () => {
    let arrowId: TLShapeId
    if (existing) {
      arrowId = existing.id
      editor.updateShape<TLArrowShape>({
        id: arrowId,
        type: 'arrow',
        props: { richText: label },
        meta: { ...existing.meta, kind: 'edge', planId: key, diagram: edge.diagram, srcStatus, dstStatus },
      })
    } else {
      arrowId = edgeShapeId(edge.src, edge.dst)
      const a = centerOf(from)
      const b = centerOf(to)
      editor.createShape<TLArrowShape>({
        id: arrowId,
        type: 'arrow',
        parentId: from.parentId === frame.id ? frame.id : editor.getCurrentPageId(),
        x: a.x,
        y: a.y,
        props: {
          kind: 'elbow',
          start: { x: 0, y: 0 },
          end: { x: b.x - a.x, y: b.y - a.y },
          arrowheadStart: 'none',
          arrowheadEnd: 'arrow',
          color: 'black',
          size: 's',
          font: 'sans',
          richText: label,
        },
        meta: shapeMeta('edge', key, { diagram: edge.diagram, hidden: !!frame.meta.collapsed, srcStatus, dstStatus }),
      })
    }
    ensureBindings(editor, arrowId, edge.src, edge.dst, from.id, to.id)
  })
}

export function deleteEdge(editor: Editor, src: string, dst: string): void {
  const shape = findEdgeShape(editor, src, dst)
  if (shape && kindOf(shape) === 'edge') remote(editor, () => editor.deleteShapes([shape.id]))
}

// ---------- agents ----------

function agentDetail(agent: AgentCard): string {
  const note = (agent.notes ?? '').split('\n').find((l) => l.trim())
  if (note) return note.trim()
  if (agent.claude_agent_ref) return `ref ${agent.claude_agent_ref}`
  return ''
}

export function upsertAgent(editor: Editor, agent: AgentCard, ctx: NodeContext, pos?: { x: number; y: number; w?: number; h?: number }): void {
  const node = agent.assigned_node ? ctx.byId.get(agent.assigned_node) : undefined
  const ready = node ? node.depends_on.every((d) => ctx.byId.get(d)?.status === 'done') : false
  const existing = findAgentShape(editor, agent.id)
  const props = {
    agentId: agent.id,
    name: agent.id,
    status: agent.status,
    detail: agentDetail(agent),
    ready,
    nodeId: agent.assigned_node ?? '',
  }
  remote(editor, () => {
    if (existing) {
      editor.updateShape<AgentCardShape>({ id: existing.id, type: 'agent-card', props })
      return
    }
    const p: { x: number; y: number; w?: number; h?: number } = pos ?? nextAgentSlot(editor)
    editor.createShape<AgentCardShape>({
      id: agentShapeId(agent.id),
      type: 'agent-card',
      x: p.x,
      y: p.y,
      props: { ...props, w: p.w ?? AGENT_CARD_W, h: p.h ?? AGENT_CARD_H },
      meta: shapeMeta('agent-card', agent.id),
    })
  })
}

function nextAgentSlot(editor: Editor): { x: number; y: number } {
  const frame = getFrame(editor)
  const fx = frame ? frame.x + frame.props.w + AGENT_GAP : DEFAULT_FRAME.w + AGENT_GAP
  const fy = frame ? frame.y : 0
  let count = 0
  for (const s of editor.getCurrentPageShapes()) if (kindOf(s) === 'agent-card') count++
  return { x: fx, y: fy + count * (AGENT_CARD_H + 20) }
}

export function deleteAgent(editor: Editor, id: string): void {
  const shape = findAgentShape(editor, id)
  if (shape && kindOf(shape) === 'agent-card') remote(editor, () => editor.deleteShapes([shape.id]))
}

// ---------- positions ----------

function layoutInputs(nodes: PlanNode[], edges: PlanEdge[]): { ns: LayoutNodeInput[]; es: LayoutEdgeInput[] } {
  return {
    ns: nodes.map((n) => ({ id: n.id, w: NODE_W, h: NODE_H })),
    es: edges.map((e) => ({ from: e.src, to: e.dst })),
  }
}

/** frame-local positions currently on canvas or in the layout sidecar (page → local) */
function knownPositions(editor: Editor, nodes: PlanNode[], layout: Layout | undefined): Record<string, ExistingPosition> {
  const origin = frameOrigin(editor)
  const out: Record<string, ExistingPosition> = {}
  for (const n of nodes) {
    const shape = findNodeShape(editor, n.id)
    if (shape) {
      out[n.id] = { x: shape.x, y: shape.y, pinned: !!layout?.nodes?.[n.id]?.pinned }
      continue
    }
    const l = layout?.nodes?.[n.id]
    if (l && typeof l.x === 'number' && typeof l.y === 'number') {
      out[n.id] = { x: l.x - origin.x, y: l.y - origin.y, pinned: !!l.pinned }
    }
  }
  return out
}

/** dagre only for nodes with no known position (never relayouts on push) */
export function placeNodes(editor: Editor, nodes: PlanNode[], edges: PlanEdge[], layout: Layout | undefined, direction?: string): Record<string, { x: number; y: number }> {
  const { ns, es } = layoutInputs(nodes, edges)
  return positionsForMissing(ns, es, knownPositions(editor, nodes, layout), direction ?? layout?.direction)
}

// ---------- snapshot ----------

export function applySnapshot(editor: Editor, snap: PlanSnapshot): void {
  const diagram = primaryDiagram(snap)
  const layout = snap.layout?.[diagram]
  const direction = snap.diagrams.find((d) => d.name === diagram)?.direction ?? layout?.direction
  const ctx = nodeContext(snap.nodes)
  const frame = ensureFrame(editor, layout?.frames?.[PLAN_FRAME_NAME])

  // nodes
  const positions = placeNodes(editor, snap.nodes, snap.edges, layout, direction)
  for (const n of snap.nodes) {
    const l = layout?.nodes?.[n.id]
    upsertNode(editor, n, ctx, { ...positions[n.id], w: l?.w, h: l?.h })
  }
  const nodeIds = new Set(snap.nodes.map((n) => n.id))
  const edgeKeys = new Set(snap.edges.map((e) => edgeKey(e.src, e.dst)))
  const agentIds = new Set(snap.agents.map((a) => a.id))
  const stale: TLShapeId[] = []
  for (const s of editor.getCurrentPageShapes()) {
    const kind = kindOf(s)
    if (!kind) continue
    const planId = String(s.meta.planId)
    if (kind === 'plan-node' && !nodeIds.has(planId) && !s.meta.provisional) stale.push(s.id)
    else if (kind === 'edge' && !edgeKeys.has(planId) && !s.meta.provisional) stale.push(s.id)
    else if (kind === 'agent-card' && !agentIds.has(planId)) stale.push(s.id)
  }
  if (stale.length) remote(editor, () => editor.deleteShapes(stale))

  // edges
  for (const e of snap.edges) upsertEdge(editor, e)
  refreshEdgeStates(editor)

  // agents
  snap.agents.forEach((a) => {
    const l = layout?.agents?.[a.id]
    upsertAgent(editor, a, ctx, l ? { x: l.x, y: l.y, w: l.w, h: l.h } : undefined)
  })

  fitFrame(editor)
  if (frame.meta.collapsed) setCollapsedVisibility(editor, true)
}

/** used by wiring for a single node push: place it if new, restyle if known */
export function upsertNodeFromState(editor: Editor, node: PlanNode, all: PlanNode[], edges: PlanEdge[], layout: Layout | undefined): void {
  const ctx = nodeContext(all)
  const existing = findNodeShape(editor, node.id)
  if (existing) {
    upsertNode(editor, node, ctx)
  } else {
    const positions = placeNodes(editor, all, edges, layout)
    upsertNode(editor, node, ctx, positions[node.id])
    fitFrame(editor)
  }
  // dependents' readiness may have changed
  for (const other of all) {
    if (other.id !== node.id && other.depends_on.includes(node.id)) upsertNode(editor, other, ctx)
  }
  refreshEdgeStates(editor)
}

/** restyle every card whose assigned node's readiness may have changed */
export function refreshAgents(editor: Editor, agents: AgentCard[], nodes: PlanNode[]): void {
  const ctx = nodeContext(nodes)
  for (const a of agents) if (findAgentShape(editor, a.id)) upsertAgent(editor, a, ctx)
}

// ---------- layout ----------

function near(a: number, b: number): boolean {
  return Math.abs(a - b) < 0.5
}

/**
 * Apply a layout sidecar (page coords). Nodes with a position are moved there; nodes whose
 * position was cleared (Re-layout) get dagre slots, pinned nodes stay.
 */
export function applyLayout(editor: Editor, layout: Layout, nodes: PlanNode[], edges: PlanEdge[]): void {
  const frame = ensureFrame(editor, layout.frames?.[PLAN_FRAME_NAME])
  const lf = layout.frames?.[PLAN_FRAME_NAME]
  if (lf) {
    const collapsed = !!lf.collapsed
    remote(editor, () => {
      const patch: { x?: number; y?: number } = {}
      if (typeof lf.x === 'number' && !near(lf.x, frame.x)) patch.x = lf.x
      if (typeof lf.y === 'number' && !near(lf.y, frame.y)) patch.y = lf.y
      const w = typeof lf.w === 'number' ? lf.w : frame.props.w
      const expandedH = typeof lf.h === 'number' ? lf.h : Number(frame.meta.expandedH ?? frame.props.h)
      editor.updateShape<TLFrameShape>({
        id: frame.id,
        type: 'frame',
        ...patch,
        props: { w, h: collapsed ? COLLAPSED_H : expandedH },
        meta: { ...frame.meta, collapsed, expandedH },
      })
    })
    if (collapsed !== !!frame.meta.collapsed) setCollapsedVisibility(editor, collapsed)
  }
  const origin = frameOrigin(editor)
  const { ns, es } = layoutInputs(nodes, edges)
  const existing: Record<string, ExistingPosition> = {}
  for (const n of nodes) {
    const l = layout.nodes?.[n.id]
    if (l && typeof l.x === 'number' && typeof l.y === 'number') existing[n.id] = { x: l.x - origin.x, y: l.y - origin.y, pinned: true }
  }
  const positions = relayoutUnpinned(ns, es, existing, layout.direction)
  remote(editor, () => {
    for (const n of nodes) {
      const shape = findNodeShape(editor, n.id)
      const p = positions[n.id]
      if (!shape || !p) continue
      const l = layout.nodes?.[n.id]
      const w = l?.w ?? shape.props.w
      const h = l?.h ?? shape.props.h
      if (near(shape.x, p.x) && near(shape.y, p.y) && near(w, shape.props.w) && near(h, shape.props.h)) continue
      editor.updateShape({ id: shape.id, type: shape.type, x: p.x, y: p.y, props: { w, h } } as TLShapePartial)
    }
    for (const [id, l] of Object.entries(layout.agents ?? {})) {
      const shape = findAgentShape(editor, id)
      if (!shape || typeof l.x !== 'number' || typeof l.y !== 'number') continue
      if (near(shape.x, l.x) && near(shape.y, l.y)) continue
      editor.updateShape<AgentCardShape>({ id: shape.id, type: 'agent-card', x: l.x, y: l.y })
    }
  })
  const bounds = boundsOf(ns, positions)
  const f = getFrame(editor)
  if (f && !f.meta.collapsed && (bounds.w > f.props.w || bounds.h > f.props.h)) {
    remote(editor, () => editor.updateShape<TLFrameShape>({ id: f.id, type: 'frame', props: { w: Math.max(f.props.w, bounds.w), h: Math.max(f.props.h, bounds.h) } }))
  }
  fitFrame(editor)
}

/** Local re-layout of unpinned nodes (used right after sending plan.relayout). */
export function relayoutLocal(editor: Editor, nodes: PlanNode[], edges: PlanEdge[], layout: Layout | undefined): void {
  const pinned: NonNullable<Layout['nodes']> = {}
  for (const [id, l] of Object.entries(layout?.nodes ?? {})) if (l.pinned) pinned[id] = l
  applyLayout(editor, { direction: layout?.direction, nodes: pinned }, nodes, edges)
}

// ---------- zoom adaptation ----------

/**
 * Write `--wb-zoom` (and the derived border alpha) on the editor container, at camera *stop* only
 * — n8n's zoom-adaptive borders and Dagster's degrade-to-dot both read them. Returns a disposer.
 */
export function installZoomTracking(editor: Editor): () => void {
  const el = editor.getContainer()
  const write = (zoom: number) => {
    el.style.setProperty('--wb-zoom', String(zoom))
    el.style.setProperty('--wb-border-alpha', borderAlpha(zoom).toFixed(3))
    zoomAtom.set(zoom)
  }
  return react('wb-zoom', () => {
    const zoom = editor.getZoomLevel()
    // reading both keeps the reaction subscribed; we only publish once the camera settles
    if (editor.getCameraState() !== 'idle') return
    write(zoom)
  })
}

// ---------- collapse ----------

export function setCollapsedVisibility(editor: Editor, hidden: boolean): void {
  const frame = getFrame(editor)
  if (!frame) return
  remote(editor, () => {
    for (const cid of editor.getSortedChildIdsForParent(frame.id)) {
      const s = editor.getShape(cid)
      if (!s) continue
      if (!!s.meta.hidden === hidden) continue
      editor.updateShape({ id: s.id, type: s.type, meta: { ...s.meta, hidden } })
    }
  })
}

export function setFrameCollapsed(editor: Editor, collapsed: boolean): LayoutFrame | null {
  const frame = getFrame(editor)
  if (!frame) return null
  if (!!frame.meta.collapsed === collapsed) return null
  const expandedH = collapsed ? frame.props.h : Number(frame.meta.expandedH ?? DEFAULT_FRAME.h)
  remote(editor, () => {
    editor.updateShape<TLFrameShape>({
      id: frame.id,
      type: 'frame',
      props: { h: collapsed ? COLLAPSED_H : expandedH },
      meta: { ...frame.meta, collapsed, expandedH },
    })
  })
  setCollapsedVisibility(editor, collapsed)
  const f = getFrame(editor)!
  return { x: f.x, y: f.y, w: f.props.w, h: expandedH, collapsed }
}
