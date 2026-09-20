// Server truth → tldraw store. Every write is tagged remote (so the user-edit listener stays
// quiet) and history-ignored (no undo pollution). Every function is idempotent.
//
// B.5: the single plan frame is gone. Nodes live in one of three board frames (HLD / LLD / ER)
// on the one page, routed by `node.type`; everything that used to be a singleton now takes a
// `frameId`.
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
  type TLParentId,
  type TLShape,
  type TLShapeId,
  type TLShapePartial,
} from 'tldraw'
import { AGENT_CARD_H, AGENT_CARD_W, agentCardFacts } from '../shapes/agentCard'
import type { AgentCardShape } from '../shapes/AgentCardUtil'
import {
  FOLDER_COLLAPSED_H,
  FOLDER_H,
  FOLDER_W,
  folderCollapsePatch,
  folderMermaid,
} from '../shapes/agentFolder'
import type { AgentFolderShape } from '../shapes/AgentFolderUtil'
import { PLAN_NODE_H, PLAN_NODE_W, type PlanNodeShape } from '../shapes/PlanNodeUtil'
import { borderAlpha, zoomAtom } from './zoom'
import {
  BOARDS,
  COLLAPSED_BOARD_H,
  boardAtPoint,
  boardFrameId,
  boardLayoutKey,
  boardOf,
  boardOfFrameId,
  boardOfShape,
  boardSpecsFor,
  edgeScope,
  ensureBoards,
  notifyBoardReparent,
  presentBoards,
  type BoardName,
} from './boards'
import {
  agentShapeId,
  bindingId,
  edgeShapeId,
  folderShapeId,
  nodeShapeId,
  parseEdgeKey,
  shapeMeta,
  type ShapeKind,
} from '../shapes/ids'
import { FRAME_PAD, NODE_H, NODE_W, boundsOf, positionsForMissing, relayoutUnpinned, type ExistingPosition, type LayoutEdgeInput, type LayoutNodeInput } from '../layout/dagre'
import type { AgentCard, Layout, LayoutFrame, LayoutPatch, NodeStatus, PlanEdge, PlanNode, PlanSnapshot } from '../state/types'
import { edgeKey, nodeReady } from '../state/types'

export const COLLAPSED_H = COLLAPSED_BOARD_H
export const AGENT_GAP = 60
/** the plan-node label hangs below the box (B.3); reserve room for it when growing the frame */
export const PLAN_NODE_LABEL_H = 40

export function remote(editor: Editor, fn: () => void): void {
  editor.store.mergeRemoteChanges(() => editor.run(fn, { history: 'ignore' }))
}

export function kindOf(shape: TLShape | undefined): ShapeKind | null {
  const k = shape?.meta?.kind
  return k === 'plan-node' || k === 'agent-card' || k === 'plan-frame' || k === 'edge' || k === 'agent-folder' ? k : null
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

export function getFrame(editor: Editor, frameId: TLShapeId): TLFrameShape | undefined {
  return editor.getShape<TLFrameShape>(frameId)
}

// ---------- boards ----------

/** create the board frames a snapshot calls for (ER only when it has an `er` diagram) */
export function ensureBoardFrames(editor: Editor, diagramNames: Iterable<string> = [], layouts?: Record<string, Layout>): BoardName[] {
  const frames: Record<string, LayoutFrame | undefined> = {}
  for (const b of BOARDS) frames[boardLayoutKey(b.name)] = layouts?.[b.name]?.frames?.[boardLayoutKey(b.name)]
  ensureBoards(editor, boardSpecsFor(diagramNames), frames, (fn) => remote(editor, fn))
  return presentBoards(editor)
}

/** the board a node belongs on, creating the default frames when the page is still empty */
export function boardForNode(editor: Editor, node: PlanNode): { board: BoardName; frameId: TLShapeId } {
  let present = presentBoards(editor)
  if (present.length === 0) present = ensureBoardFrames(editor)
  const board = boardOf(node, present)
  return { board, frameId: boardFrameId(board) }
}

export function frameOrigin(editor: Editor, frameId: TLShapeId): { x: number; y: number } {
  const f = getFrame(editor, frameId)
  if (f) return { x: f.x, y: f.y }
  const spec = BOARDS.find((b) => boardFrameId(b.name) === frameId)
  return spec ? { x: spec.x, y: spec.y } : { x: 0, y: 0 }
}

/** grow (never shrink) the frame so every child fits, unless collapsed */
export function fitFrame(editor: Editor, frameId: TLShapeId): void {
  const frame = getFrame(editor, frameId)
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

export function fitAllFrames(editor: Editor): void {
  for (const board of presentBoards(editor)) fitFrame(editor, boardFrameId(board))
}

// ---------- nodes ----------

export interface NodeContext {
  byId: Map<string, PlanNode>
}

export function nodeContext(nodes: Iterable<PlanNode>): NodeContext {
  return { byId: new Map(Array.from(nodes, (n) => [n.id, n])) }
}

/** keep a re-parented node inside its new board */
function clampIntoFrame(editor: Editor, frameId: TLShapeId, x: number, y: number, w: number, h: number): { x: number; y: number } {
  const frame = getFrame(editor, frameId)
  if (!frame) return { x, y }
  const maxX = Math.max(FRAME_PAD, frame.props.w - w - FRAME_PAD)
  const maxY = Math.max(FRAME_PAD, frame.props.h - h - PLAN_NODE_LABEL_H - FRAME_PAD)
  return { x: Math.min(Math.max(x, FRAME_PAD), maxX), y: Math.min(Math.max(y, FRAME_PAD), maxY) }
}

/**
 * The node's `type` changed, so it now belongs on another board: move the shape into that frame
 * (tldraw's `reparentShapes` converts the point through `getShapePageTransform`), clamp it inside
 * and hand the layout entry to the new board's sidecar.
 */
function reparentNode(editor: Editor, shape: NodeShape, board: BoardName, frameId: TLShapeId, planId: string): void {
  remote(editor, () => {
    editor.reparentShapes([shape.id], frameId)
    const moved = editor.getShape(shape.id) as NodeShape | undefined
    if (!moved) return
    const p = clampIntoFrame(editor, frameId, moved.x, moved.y, moved.props.w, moved.props.h)
    if (p.x !== moved.x || p.y !== moved.y) {
      editor.updateShape({ id: moved.id, type: moved.type, x: p.x, y: p.y } as TLShapePartial)
    }
  })
  const now = editor.getShape(shape.id) as NodeShape | undefined
  if (!now) return
  const origin = frameOrigin(editor, frameId)
  notifyBoardReparent(board, {
    nodes: { [planId]: { x: origin.x + now.x, y: origin.y + now.y, w: now.props.w, h: now.props.h, parent: boardLayoutKey(board), pinned: true } },
  })
}

/** `pos` is frame-local (top-left). Only used on create; existing shapes never move here. */
export function upsertNode(editor: Editor, node: PlanNode, ctx: NodeContext, pos?: { x: number; y: number; w?: number; h?: number }): void {
  const { board, frameId } = boardForNode(editor, node)
  const frame = getFrame(editor, frameId)
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
      parentId: frameId,
      x: p.x,
      y: p.y,
      props: { ...props, w: p.w ?? PLAN_NODE_W, h: p.h ?? PLAN_NODE_H },
      meta: shapeMeta('plan-node', node.id, { status: node.status, board, hidden: !!frame?.meta.collapsed }),
    })
  })
  // the type changed under us: the node moves to its new board (and its sidecar entry with it)
  if (existing && boardOfFrameId(String(existing.parentId)) && String(existing.parentId) !== String(frameId)) {
    reparentNode(editor, existing, board, frameId, node.id)
    remote(editor, () => {
      const s = editor.getShape(existing.id)
      if (s) editor.updateShape({ id: s.id, type: s.type, meta: { ...s.meta, board } } as TLShapePartial)
    })
  }
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

/** centre of a shape expressed in `parentId`'s space (page space when the parent is the page) */
function centerIn(editor: Editor, shape: TLShape, parentId: TLParentId): { x: number; y: number } {
  const b = editor.getShapePageBounds(shape.id)
  const w = 'w' in shape.props ? (shape.props as { w: number }).w : 0
  const h = 'h' in shape.props ? (shape.props as { h: number }).h : 0
  const center = b ? { x: b.x + b.w / 2, y: b.y + b.h / 2 } : { x: shape.x + w / 2, y: shape.y + h / 2 }
  if (boardOfFrameId(String(parentId))) {
    const o = frameOrigin(editor, parentId as TLShapeId)
    return { x: center.x - o.x, y: center.y - o.y }
  }
  return center
}

/** B.4: endpoints in different frames are parented to the page and drawn as secondary */
function edgeParent(editor: Editor, from: TLShape, to: TLShape): { parentId: TLParentId; cross: boolean } {
  const cross = edgeScope(boardOfShape(from), boardOfShape(to)) === 'cross'
  return { parentId: cross ? editor.getCurrentPageId() : from.parentId, cross }
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

/**
 * Re-stamp every edge whose endpoints' statuses or boards moved (cheap: only changed metas are
 * written). Must run after any batch of node upserts — including a re-parent, which can turn a
 * within-board edge into a cross-board one.
 */
export function refreshEdgeStates(editor: Editor): void {
  const patches: TLShapePartial[] = []
  const reparents: Array<[TLShapeId, TLParentId]> = []
  for (const s of editor.getCurrentPageShapes()) {
    if (kindOf(s) !== 'edge') continue
    const pair = parseEdgeKey(String(s.meta.planId))
    if (!pair) continue
    const from = findNodeShape(editor, pair.src)
    const to = findNodeShape(editor, pair.dst)
    const srcStatus = statusOf(from)
    const dstStatus = statusOf(to)
    let cross = !!s.meta.cross
    if (from && to) {
      const p = edgeParent(editor, from, to)
      cross = p.cross
      if (String(s.parentId) !== String(p.parentId)) reparents.push([s.id, p.parentId])
    }
    if (s.meta.srcStatus === srcStatus && s.meta.dstStatus === dstStatus && !!s.meta.cross === cross) continue
    patches.push({ id: s.id, type: s.type, meta: { ...s.meta, srcStatus, dstStatus, cross } } as TLShapePartial)
  }
  if (patches.length || reparents.length) {
    remote(editor, () => {
      for (const [id, parentId] of reparents) editor.reparentShapes([id], parentId)
      if (patches.length) editor.updateShapes(patches)
    })
  }
}

export function upsertEdge(editor: Editor, edge: PlanEdge): void {
  const from = findNodeShape(editor, edge.src)
  const to = findNodeShape(editor, edge.dst)
  if (!from || !to) return
  const existing = findEdgeShape(editor, edge.src, edge.dst)
  const { parentId, cross } = edgeParent(editor, from, to)
  const key = edgeKey(edge.src, edge.dst)
  const label = toRichText(edge.label ?? '')
  const srcStatus = statusOf(from)
  const dstStatus = statusOf(to)
  const parentFrame = cross ? undefined : getFrame(editor, parentId as TLShapeId)
  remote(editor, () => {
    let arrowId: TLShapeId
    if (existing) {
      arrowId = existing.id
      if (String(existing.parentId) !== String(parentId)) editor.reparentShapes([arrowId], parentId)
      editor.updateShape<TLArrowShape>({
        id: arrowId,
        type: 'arrow',
        props: { richText: label },
        meta: { ...existing.meta, kind: 'edge', planId: key, diagram: edge.diagram, srcStatus, dstStatus, cross },
      })
    } else {
      arrowId = edgeShapeId(edge.src, edge.dst)
      const a = centerIn(editor, from, parentId)
      const b = centerIn(editor, to, parentId)
      editor.createShape<TLArrowShape>({
        id: arrowId,
        type: 'arrow',
        parentId,
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
        meta: shapeMeta('edge', key, { diagram: edge.diagram, hidden: !!parentFrame?.meta.collapsed, srcStatus, dstStatus, cross }),
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
    detail: agentDetail(agent),
    ready,
    nodeId: agent.assigned_node ?? '',
    // live monitoring (B.7): flat primitives, because tldraw validates each prop on its own
    ...agentCardFacts(agent),
  }
  const ownerFrameId = node ? boardForNode(editor, node).frameId : boardFrameId(presentBoards(editor)[0] ?? 'hld')
  remote(editor, () => {
    if (existing) {
      editor.updateShape<AgentCardShape>({ id: existing.id, type: 'agent-card', props })
      return
    }
    const p: { x: number; y: number; w?: number; h?: number } = pos ?? nextAgentSlot(editor, ownerFrameId)
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

/** cards sit in a row under their owner board, so they never land on the next board */
export function nextAgentSlot(editor: Editor, ownerFrameId: TLShapeId): { x: number; y: number } {
  const frame = getFrame(editor, ownerFrameId)
  const origin = frameOrigin(editor, ownerFrameId)
  const y = origin.y + (frame ? frame.props.h : 0) + AGENT_GAP
  let count = 0
  for (const s of editor.getCurrentPageShapes()) {
    if (kindOf(s) !== 'agent-card') continue
    if (Math.abs(s.y - y) < 1) count++
  }
  return { x: origin.x + count * (AGENT_CARD_W + 20), y }
}

export function deleteAgent(editor: Editor, id: string): void {
  const shape = findAgentShape(editor, id)
  if (shape && kindOf(shape) === 'agent-card') remote(editor, () => editor.deleteShapes([shape.id]))
}

// ---------- agent folders (B.6) ----------

export const FOLDER_GAP = 24

export function findFolderShape(editor: Editor, agentId: string): AgentFolderShape | undefined {
  return editor.getShape<AgentFolderShape>(folderShapeId(agentId))
}

/** the saved `frames['<agent-id>']` entry, whichever board's sidecar happens to hold it */
export function folderLayout(layouts: Record<string, Layout> | undefined, agentId: string): LayoutFrame | undefined {
  for (const l of Object.values(layouts ?? {})) {
    const f = l.frames?.[agentId]
    if (f) return f
  }
  return undefined
}

/**
 * Folders sit in a row under the agent cards of their owner board — the inter-board gutter is
 * only 200px, so anything placed to the RIGHT of a board would land on the next one (B.S2's
 * hand-off note; B.6's "to the right of its owned node" is the one deviation here).
 */
export function nextFolderSlot(editor: Editor, ownerFrameId: TLShapeId): { x: number; y: number } {
  const frame = getFrame(editor, ownerFrameId)
  const origin = frameOrigin(editor, ownerFrameId)
  const y = origin.y + (frame ? frame.props.h : 0) + AGENT_GAP + AGENT_CARD_H + FOLDER_GAP
  let count = 0
  for (const s of editor.getCurrentPageShapes()) {
    if (kindOf(s) !== 'agent-folder') continue
    if (Math.abs(s.y - y) < 1) count++
  }
  return { x: origin.x + count * (FOLDER_W + 20), y }
}

/** the board whose sidecar owns this folder: its node's board, else the first board present */
function folderBoard(editor: Editor, node: PlanNode | undefined): BoardName {
  if (node) return boardForNode(editor, node).board
  return presentBoards(editor)[0] ?? 'hld'
}

/**
 * One folder per agent, created on the first snapshot that mentions it and updated in place
 * afterwards. `saved` is the sidecar entry (`frames['agent-parser']`), which carries both the
 * geometry and the collapse flag.
 */
export function upsertAgentFolder(editor: Editor, agent: AgentCard, ctx: NodeContext, saved?: LayoutFrame): void {
  const node = agent.assigned_node ? ctx.byId.get(agent.assigned_node) : undefined
  const board = folderBoard(editor, node)
  const existing = findFolderShape(editor, agent.id)
  const props = {
    agentId: agent.id,
    ownerNode: agent.assigned_node ?? '',
    status: agent.status,
    planMd: agent.plan_md ?? '',
    mermaid: folderMermaid(agent),
    diagramError: String(agent.diagram?.error ?? ''),
  }
  remote(editor, () => {
    if (existing) {
      editor.updateShape<AgentFolderShape>({ id: existing.id, type: 'agent-folder', props })
      return
    }
    const slot = nextFolderSlot(editor, boardFrameId(board))
    const collapsed = !!saved?.collapsed
    const w = typeof saved?.w === 'number' ? saved.w : FOLDER_W
    const expandedH = typeof saved?.h === 'number' ? saved.h : FOLDER_H
    editor.createShape<AgentFolderShape>({
      id: folderShapeId(agent.id),
      type: 'agent-folder',
      x: typeof saved?.x === 'number' ? saved.x : slot.x,
      y: typeof saved?.y === 'number' ? saved.y : slot.y,
      props: { ...props, w, h: collapsed ? FOLDER_COLLAPSED_H : expandedH },
      meta: shapeMeta('agent-folder', agent.id, {
        ownerNode: agent.assigned_node ?? '',
        board,
        hidden: false,
        collapsed,
        expandedH,
      }),
    })
  })
}

export function deleteAgentFolder(editor: Editor, agentId: string): void {
  const shape = findFolderShape(editor, agentId)
  if (shape) remote(editor, () => editor.deleteShapes([shape.id]))
}

/**
 * Collapse/expand one folder. Same primitive as a board frame — `meta.collapsed` on the folder,
 * `meta.hidden` on its children, the height shrinks to 36px and `meta.expandedH` remembers the
 * old one — and it returns the sidecar entry to persist under the agent's own key.
 */
export function setFolderCollapsed(editor: Editor, folderId: TLShapeId, collapsed: boolean): LayoutFrame | null {
  const folder = editor.getShape<AgentFolderShape>(folderId)
  if (!folder || kindOf(folder) !== 'agent-folder') return null
  if (!!folder.meta.collapsed === collapsed) return null
  const expandedH = Number(folder.meta.expandedH ?? FOLDER_H)
  const patch = folderCollapsePatch({ x: folder.x, y: folder.y, w: folder.props.w, h: folder.props.h, expandedH }, collapsed)
  remote(editor, () => {
    editor.updateShape<AgentFolderShape>({
      id: folder.id,
      type: 'agent-folder',
      props: { h: collapsed ? FOLDER_COLLAPSED_H : patch.h },
      meta: { ...folder.meta, collapsed, expandedH: patch.h },
    })
  })
  // folders hold their editors in the shape itself, but a child dropped into one still hides
  setCollapsedVisibility(editor, folderId, collapsed)
  return patch
}

/** the card's "Open folder" button: expand it if it is shut, then put the camera on it */
export function revealAgentFolder(editor: Editor, agentId: string): boolean {
  const folder = findFolderShape(editor, agentId)
  if (!folder) return false
  if (folder.meta.collapsed) notifyFolderCollapse(editor, folder.id, false)
  editor.select(folder.id)
  editor.zoomToSelection({ animation: { duration: 250 } })
  return true
}

/** `(board, patch)` — the folder's collapse flag belongs to its owner board's sidecar */
export type FolderCollapseListener = (board: string, patch: LayoutPatch) => void
const folderListeners = new Set<FolderCollapseListener>()

/** kept here (not in frame.tsx) so `revealAgentFolder` can persist an expand too */
export function onFolderCollapse(fn: FolderCollapseListener): () => void {
  folderListeners.add(fn)
  return () => folderListeners.delete(fn)
}

export function notifyFolderCollapse(editor: Editor, folderId: TLShapeId, collapsed: boolean): void {
  const folder = editor.getShape<AgentFolderShape>(folderId)
  if (!folder) return
  const agentId = String(folder.meta.planId)
  const board = String(folder.meta.board ?? presentBoards(editor)[0] ?? 'hld')
  const patch = setFolderCollapsed(editor, folderId, collapsed)
  if (!patch) return
  folderListeners.forEach((l) => l(board, { frames: { [agentId]: patch } }))
}

// ---------- positions ----------

function layoutInputs(nodes: PlanNode[], edges: PlanEdge[]): { ns: LayoutNodeInput[]; es: LayoutEdgeInput[] } {
  return {
    ns: nodes.map((n) => ({ id: n.id, w: NODE_W, h: NODE_H })),
    es: edges.map((e) => ({ from: e.src, to: e.dst })),
  }
}

/** group the plan's nodes by the board they are homed on */
export function nodesByBoard(nodes: PlanNode[], present: Iterable<string>): Map<BoardName, PlanNode[]> {
  const out = new Map<BoardName, PlanNode[]>()
  const list = Array.from(present)
  for (const n of nodes) {
    const b = boardOf(n, list)
    const arr = out.get(b)
    if (arr) arr.push(n)
    else out.set(b, [n])
  }
  return out
}

/** edges with both endpoints on the same board — the only ones dagre should rank */
function edgesWithin(edges: PlanEdge[], ids: Set<string>): PlanEdge[] {
  return edges.filter((e) => ids.has(e.src) && ids.has(e.dst))
}

/** frame-local positions currently on canvas or in the layout sidecar (page → local) */
function knownPositions(editor: Editor, frameId: TLShapeId, nodes: PlanNode[], layout: Layout | undefined): Record<string, ExistingPosition> {
  const origin = frameOrigin(editor, frameId)
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

/** dagre only for nodes with no known position (never relayouts on push), one board at a time */
export function placeNodes(editor: Editor, frameId: TLShapeId, nodes: PlanNode[], edges: PlanEdge[], layout: Layout | undefined, direction?: string): Record<string, { x: number; y: number }> {
  const ids = new Set(nodes.map((n) => n.id))
  const { ns, es } = layoutInputs(nodes, edgesWithin(edges, ids))
  return positionsForMissing(ns, es, knownPositions(editor, frameId, nodes, layout), direction ?? layout?.direction)
}

// ---------- snapshot ----------

function agentLayout(layouts: Record<string, Layout> | undefined, id: string) {
  for (const l of Object.values(layouts ?? {})) {
    const a = l.agents?.[id]
    if (a) return a
  }
  return undefined
}

export function applySnapshot(editor: Editor, snap: PlanSnapshot): void {
  const present = ensureBoardFrames(editor, snap.diagrams.map((d) => d.name), snap.layout)
  const ctx = nodeContext(snap.nodes)

  // nodes, board by board (each board's dagre run is independent)
  for (const [board, list] of nodesByBoard(snap.nodes, present)) {
    const frameId = boardFrameId(board)
    const layout = snap.layout?.[board]
    const direction = snap.diagrams.find((d) => d.name === board)?.direction ?? layout?.direction
    const positions = placeNodes(editor, frameId, list, snap.edges, layout, direction)
    for (const n of list) {
      const l = layout?.nodes?.[n.id]
      upsertNode(editor, n, ctx, { ...positions[n.id], w: l?.w, h: l?.h })
    }
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
    else if (kind === 'agent-folder' && !agentIds.has(planId)) stale.push(s.id)
  }
  if (stale.length) remote(editor, () => editor.deleteShapes(stale))

  // edges
  for (const e of snap.edges) upsertEdge(editor, e)
  refreshEdgeStates(editor)

  // agents
  snap.agents.forEach((a) => {
    const l = agentLayout(snap.layout, a.id)
    upsertAgent(editor, a, ctx, l ? { x: l.x, y: l.y, w: l.w, h: l.h } : undefined)
    // B.6: one folder per agent, geometry and collapse flag from `frames['<agent-id>']`
    upsertAgentFolder(editor, a, ctx, folderLayout(snap.layout, a.id))
  })

  for (const board of present) {
    const frameId = boardFrameId(board)
    fitFrame(editor, frameId)
    if (getFrame(editor, frameId)?.meta.collapsed) setCollapsedVisibility(editor, frameId, true)
  }
}

/** used by wiring for a single node push: place it if new, restyle (and re-home) if known */
export function upsertNodeFromState(editor: Editor, node: PlanNode, all: PlanNode[], edges: PlanEdge[], layouts: Record<string, Layout> | undefined): void {
  const ctx = nodeContext(all)
  const existing = findNodeShape(editor, node.id)
  if (existing) {
    upsertNode(editor, node, ctx)
  } else {
    const { board, frameId } = boardForNode(editor, node)
    const layout = layouts?.[board]
    const mine = all.filter((n) => boardOf(n, presentBoards(editor)) === board)
    const positions = placeNodes(editor, frameId, mine, edges, layout)
    upsertNode(editor, node, ctx, positions[node.id])
    fitFrame(editor, frameId)
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

/** a card push also refreshes the folder's plan/diagram text and hue */
export function upsertAgentAndFolder(editor: Editor, agent: AgentCard, ctx: NodeContext, pos?: { x: number; y: number; w?: number; h?: number }, saved?: LayoutFrame): void {
  upsertAgent(editor, agent, ctx, pos)
  upsertAgentFolder(editor, agent, ctx, saved)
}

// ---------- layout ----------

function near(a: number, b: number): boolean {
  return Math.abs(a - b) < 0.5
}

/**
 * Apply one board's layout sidecar (page coords). Nodes with a position are moved there; nodes
 * whose position was cleared (Re-layout) get dagre slots, pinned nodes stay.
 */
export function applyLayout(editor: Editor, board: string, layout: Layout, nodes: PlanNode[], edges: PlanEdge[]): void {
  const frameId = boardFrameId(board)
  if (!getFrame(editor, frameId)) ensureBoardFrames(editor, [board])
  const frame = getFrame(editor, frameId)
  if (!frame) return
  const key = boardLayoutKey(board)
  const lf = layout.frames?.[key]
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
    if (collapsed !== !!frame.meta.collapsed) setCollapsedVisibility(editor, frameId, collapsed)
  }
  const mine = nodes.filter((n) => boardOf(n, presentBoards(editor)) === board)
  const ids = new Set(mine.map((n) => n.id))
  const origin = frameOrigin(editor, frameId)
  const { ns, es } = layoutInputs(mine, edgesWithin(edges, ids))
  const existing: Record<string, ExistingPosition> = {}
  for (const n of mine) {
    const l = layout.nodes?.[n.id]
    if (l && typeof l.x === 'number' && typeof l.y === 'number') existing[n.id] = { x: l.x - origin.x, y: l.y - origin.y, pinned: true }
  }
  const positions = relayoutUnpinned(ns, es, existing, layout.direction)
  remote(editor, () => {
    for (const n of mine) {
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
  const f = getFrame(editor, frameId)
  if (f && !f.meta.collapsed && (bounds.w > f.props.w || bounds.h > f.props.h)) {
    remote(editor, () => editor.updateShape<TLFrameShape>({ id: f.id, type: 'frame', props: { w: Math.max(f.props.w, bounds.w), h: Math.max(f.props.h, bounds.h) } }))
  }
  fitFrame(editor, frameId)
}

/** Local re-layout of one board's unpinned nodes (used right after sending plan.relayout). */
export function relayoutLocal(editor: Editor, board: string, nodes: PlanNode[], edges: PlanEdge[], layout: Layout | undefined): void {
  const pinned: NonNullable<Layout['nodes']> = {}
  for (const [id, l] of Object.entries(layout?.nodes ?? {})) if (l.pinned) pinned[id] = l
  applyLayout(editor, board, { direction: layout?.direction, nodes: pinned }, nodes, edges)
}

// ---------- zoom adaptation ----------

/**
 * Write `--wb-zoom` (and the derived border alpha) on the editor container, at camera *stop* only
 * — n8n's zoom-adaptive borders and Dagster's degrade-to-dot both read them. The same camera-stop
 * reaction reports which board covers the viewport centre, so panning updates the board switcher
 * (B.5). Returns a disposer.
 */
export function installZoomTracking(editor: Editor, onBoard?: (board: BoardName) => void): () => void {
  const el = editor.getContainer()
  const write = (zoom: number) => {
    el.style.setProperty('--wb-zoom', String(zoom))
    el.style.setProperty('--wb-border-alpha', borderAlpha(zoom).toFixed(3))
    zoomAtom.set(zoom)
  }
  return react('wb-zoom', () => {
    const zoom = editor.getZoomLevel()
    const viewport = editor.getViewportPageBounds()
    // reading these keeps the reaction subscribed; we only publish once the camera settles
    if (editor.getCameraState() !== 'idle') return
    write(zoom)
    if (!onBoard) return
    const board = boardAtPoint(editor, { x: viewport.x + viewport.w / 2, y: viewport.y + viewport.h / 2 })
    if (board) onBoard(board)
  })
}

// ---------- collapse ----------

export function setCollapsedVisibility(editor: Editor, frameId: TLShapeId, hidden: boolean): void {
  const frame = getFrame(editor, frameId)
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

export function setFrameCollapsed(editor: Editor, frameId: TLShapeId, collapsed: boolean): LayoutFrame | null {
  const frame = getFrame(editor, frameId)
  if (!frame) return null
  if (!!frame.meta.collapsed === collapsed) return null
  const spec = BOARDS.find((b) => boardFrameId(b.name) === frameId)
  const expandedH = collapsed ? frame.props.h : Number(frame.meta.expandedH ?? spec?.h ?? frame.props.h)
  remote(editor, () => {
    editor.updateShape<TLFrameShape>({
      id: frame.id,
      type: 'frame',
      props: { h: collapsed ? COLLAPSED_H : expandedH },
      meta: { ...frame.meta, collapsed, expandedH },
    })
  })
  setCollapsedVisibility(editor, frameId, collapsed)
  const f = getFrame(editor, frameId)!
  return { x: f.x, y: f.y, w: f.props.w, h: expandedH, collapsed }
}
