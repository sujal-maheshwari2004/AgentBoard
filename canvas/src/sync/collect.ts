// Human edits → semantic ops (CONTRACTS §4) and cosmetic layout patches (§1 sidecar).
// `deriveOps` is pure over an accumulated diff plus a small context, so it is unit-testable
// without an editor. `installCollector` wires it to a live editor.
import {
  renderPlaintextFromRichText,
  type Editor,
  type RecordsDiff,
  type TLArrowBinding,
  type TLGeoShape,
  type TLRecord,
  type TLRichText,
  type TLShape,
  type TLShapeId,
  type JsonObject,
} from 'tldraw'
import { kindOf, remote, upsertEdge } from './apply'
import { boardNameFromKey } from './boards'
import { parseEdgeKey, provisionalNodeId, shapeMeta } from '../shapes/ids'
import type { EditOp, LayoutPatch, PlanEdge } from '../state/types'
import { edgeKey } from '../state/types'

export interface ArrowEnds {
  start: TLShapeId | null
  end: TLShapeId | null
}

/**
 * B.12: which kind of frame a shape sits in. `id` is the frame's `meta.planId` — the board's
 * sidecar key (`board-hld`) or the agent id for an agent folder (B.S3).
 */
export interface FrameRef {
  kind: 'board' | 'agent-folder'
  id: string
}

/** pure: resolve a parent shape to a frame ref (null for the page or anything else) */
export function frameRefOf(parent: TLShape | undefined): FrameRef | null {
  const kind = parent?.meta?.kind
  if (kind === 'plan-frame') return { kind: 'board', id: String(parent!.meta.planId) }
  if (kind === 'agent-folder') return { kind: 'agent-folder', id: String(parent!.meta.planId) }
  return null
}

export interface CollectContext {
  /** the frame a shape is parented to, or null when it is loose on the page */
  frameKindOf(shape: TLShape): FrameRef | null
  /** the active board — the fallback diagram for ops that name no board of their own */
  diagram: string
  plaintext(rt: TLRichText): string
  getShape(id: TLShapeId): TLShape | undefined
  getArrowEnds(arrowId: TLShapeId): ArrowEnds
  /** page-space top-left of a shape */
  pageXY(shape: TLShape): { x: number; y: number }
  childIds(parentId: TLShapeId): TLShapeId[]
}

export interface Pending {
  added: Map<string, TLRecord>
  updated: Map<string, [TLRecord, TLRecord]>
  removed: Map<string, TLRecord>
}

export function emptyPending(): Pending {
  return { added: new Map(), updated: new Map(), removed: new Map() }
}

export function isEmptyPending(p: Pending): boolean {
  return p.added.size === 0 && p.updated.size === 0 && p.removed.size === 0
}

/** Fold one store diff into the accumulator (added+updated → added; added+removed → nothing). */
export function mergeDiff(p: Pending, diff: RecordsDiff<TLRecord>): void {
  for (const [id, rec] of Object.entries(diff.added)) {
    p.removed.delete(id)
    p.updated.delete(id)
    p.added.set(id, rec)
  }
  for (const [id, [from, to]] of Object.entries(diff.updated)) {
    if (p.added.has(id)) {
      p.added.set(id, to)
      continue
    }
    const prev = p.updated.get(id)
    p.updated.set(id, [prev ? prev[0] : from, to])
  }
  for (const [id, rec] of Object.entries(diff.removed)) {
    if (p.added.has(id)) {
      p.added.delete(id)
      continue
    }
    p.updated.delete(id)
    p.removed.set(id, rec)
  }
}

export interface Stamp {
  id: TLShapeId
  type: TLShape['type']
  meta: JsonObject
}

export interface Derived {
  ops: EditOp[]
  stamps: Stamp[]
  layout: LayoutPatch | null
}

const isShape = (r: TLRecord): r is TLShape => r.typeName === 'shape'
const isArrowBinding = (r: TLRecord): r is TLArrowBinding => r.typeName === 'binding' && r.type === 'arrow'

export function mermaidShapeFor(geo: string): string {
  switch (geo) {
    case 'ellipse':
    case 'oval':
      return 'round'
    case 'diamond':
    case 'rhombus':
      return 'rhombus'
    case 'hexagon':
      return 'hexagon'
    default:
      return 'rect'
  }
}

function geoLabel(ctx: CollectContext, s: TLShape): string {
  if (s.type !== 'geo') return ''
  return ctx.plaintext((s as TLGeoShape).props.richText).trim()
}

function boxChanged(from: TLShape, to: TLShape): boolean {
  if (from.x !== to.x || from.y !== to.y || from.parentId !== to.parentId) return true
  const fp = from.props as { w?: number; h?: number }
  const tp = to.props as { w?: number; h?: number }
  return fp.w !== tp.w || fp.h !== tp.h
}

function nodeLayoutEntry(ctx: CollectContext, s: TLShape) {
  const p = ctx.pageXY(s)
  const props = s.props as { w?: number; h?: number }
  const frame = ctx.frameKindOf(s)
  return { x: p.x, y: p.y, w: props.w, h: props.h, parent: frame?.kind === 'board' ? frame.id : undefined, pinned: true }
}

/** the board a node shape lives on, for ops that must name one */
function boardOfShapeIn(ctx: CollectContext, s: TLShape | undefined): string | null {
  if (!s) return null
  const frame = ctx.frameKindOf(s)
  return frame?.kind === 'board' ? boardNameFromKey(frame.id) : null
}

export function deriveOps(ctx: CollectContext, pending: Pending): Derived {
  const ops: EditOp[] = []
  const stamps: Stamp[] = []
  const layout: LayoutPatch = { nodes: {}, agents: {}, frames: {} }
  let touchedLayout = false
  const touchedArrows = new Set<TLShapeId>()
  const nodeIdOf = (id: TLShapeId | null): string | null => {
    if (!id) return null
    const s = ctx.getShape(id)
    return s && kindOf(s) === 'plan-node' ? String(s.meta.planId) : null
  }

  const considerHumanGeo = (s: TLShape) => {
    if (s.type !== 'geo' || kindOf(s)) return
    // only a box drawn inside a BOARD becomes a plan node; one drawn in an agent folder (or
    // loose on the page) is ignored — the folder's own editors change agent content (B.12)
    const frame = ctx.frameKindOf(s)
    if (frame?.kind !== 'board') return
    const label = geoLabel(ctx, s)
    if (!label) return
    const id = provisionalNodeId(label)
    ops.push({ op: 'node-created', id, label, shape: mermaidShapeFor((s as TLGeoShape).props.geo), diagram: boardNameFromKey(frame.id) })
    stamps.push({ id: s.id, type: s.type, meta: shapeMeta('plan-node', id, { provisional: true }) })
    layout.nodes![id] = nodeLayoutEntry(ctx, s)
    touchedLayout = true
  }

  // added shapes
  for (const rec of pending.added.values()) {
    if (isArrowBinding(rec)) {
      touchedArrows.add(rec.fromId)
      continue
    }
    if (!isShape(rec)) continue
    if (rec.type === 'arrow') {
      touchedArrows.add(rec.id)
      continue
    }
    considerHumanGeo(rec)
  }

  // updated shapes
  for (const [from, to] of pending.updated.values()) {
    if (isArrowBinding(to)) {
      touchedArrows.add(to.fromId)
      continue
    }
    if (!isShape(to) || !isShape(from)) continue
    const kind = kindOf(to)
    if (to.type === 'arrow') {
      touchedArrows.add(to.id)
      continue
    }
    if (kind === 'plan-node') {
      const before = geoLabel(ctx, from)
      const after = geoLabel(ctx, to)
      if (before !== after && after) ops.push({ op: 'renamed', id: String(to.meta.planId), label: after })
      if (boxChanged(from, to)) {
        layout.nodes![String(to.meta.planId)] = nodeLayoutEntry(ctx, to)
        touchedLayout = true
      }
      continue
    }
    if (kind === 'agent-card') {
      if (boxChanged(from, to)) {
        const p = ctx.pageXY(to)
        const props = to.props as { w: number; h: number }
        layout.agents![String(to.meta.planId)] = { x: p.x, y: p.y, w: props.w, h: props.h }
        touchedLayout = true
      }
      continue
    }
    if (kind === 'plan-frame') {
      if (boxChanged(from, to)) {
        const props = to.props as { w: number; h: number }
        const collapsed = !!to.meta.collapsed
        layout.frames![String(to.meta.planId)] = { x: to.x, y: to.y, w: props.w, h: collapsed ? Number(to.meta.expandedH ?? props.h) : props.h, collapsed }
        touchedLayout = true
        if (from.x !== to.x || from.y !== to.y) {
          // children keep frame-local coords; their page coords moved with the frame
          for (const cid of ctx.childIds(to.id)) {
            const c = ctx.getShape(cid)
            if (c && kindOf(c) === 'plan-node') layout.nodes![String(c.meta.planId)] = nodeLayoutEntry(ctx, c)
          }
        }
      }
      continue
    }
    if (!kind) considerHumanGeo(to)
  }

  // removed shapes
  for (const rec of pending.removed.values()) {
    if (isArrowBinding(rec)) {
      touchedArrows.add(rec.fromId)
      continue
    }
    if (!isShape(rec)) continue
    const kind = kindOf(rec)
    if (kind === 'plan-node') {
      ops.push({ op: 'deleted', id: String(rec.meta.planId) })
    } else if (kind === 'edge') {
      const pair = parseEdgeKey(String(rec.meta.planId))
      if (pair) ops.push({ op: 'edge-deleted', from: pair.src, to: pair.dst, diagram: String(rec.meta.diagram ?? ctx.diagram) })
    }
    touchedArrows.delete(rec.id)
  }

  // arrows: resolve topology from live bindings at end of batch
  for (const arrowId of touchedArrows) {
    const arrow = ctx.getShape(arrowId)
    if (!arrow || arrow.type !== 'arrow') continue
    const ends = ctx.getArrowEnds(arrowId)
    const from = nodeIdOf(ends.start)
    const to = nodeIdOf(ends.end)
    if (!from || !to || from === to) continue // dangling: wait until both ends bind
    const kind = kindOf(arrow)
    if (kind === 'edge') {
      const pair = parseEdgeKey(String(arrow.meta.planId))
      if (!pair || (pair.src === from && pair.dst === to)) continue
      const diagram = String(arrow.meta.diagram ?? ctx.diagram)
      ops.push({ op: 'edge-rerouted', from: pair.src, to: pair.dst, new_from: from, new_to: to, diagram })
      stamps.push({ id: arrow.id, type: 'arrow', meta: { ...arrow.meta, planId: edgeKey(from, to) } })
    } else if (!kind) {
      const label = ctx.plaintext((arrow.props as { richText: TLRichText }).richText).trim()
      const diagram = boardOfShapeIn(ctx, ends.start ? ctx.getShape(ends.start) : undefined) ?? ctx.diagram
      const op: EditOp = { op: 'edge-created', from, to, diagram }
      if (label) op.label = label
      ops.push(op)
      stamps.push({ id: arrow.id, type: 'arrow', meta: shapeMeta('edge', edgeKey(from, to), { diagram, provisional: true }) })
    }
  }

  return { ops, stamps, layout: touchedLayout ? layout : null }
}

export function mergeLayoutPatch(into: LayoutPatch, patch: LayoutPatch): LayoutPatch {
  return {
    ...into,
    nodes: { ...(into.nodes ?? {}), ...(patch.nodes ?? {}) },
    agents: { ...(into.agents ?? {}), ...(patch.agents ?? {}) },
    frames: { ...(into.frames ?? {}), ...(patch.frames ?? {}) },
    edges: { ...(into.edges ?? {}), ...(patch.edges ?? {}) },
  }
}

// ---------- live wiring ----------

export interface CollectorOptions {
  getDiagram(): string
  getEdges(): PlanEdge[]
  onOps(ops: EditOp[]): void
  onLayout(patch: LayoutPatch): void
  opsDebounceMs?: number
  layoutDebounceMs?: number
}

export function liveContext(editor: Editor, diagram: string): CollectContext {
  return {
    frameKindOf: (shape) => frameRefOf(editor.getShape(shape.parentId as TLShapeId)),
    diagram,
    plaintext: (rt) => renderPlaintextFromRichText(editor, rt),
    getShape: (id) => editor.getShape(id),
    getArrowEnds: (arrowId) => {
      const bs = editor.getBindingsFromShape<TLArrowBinding>(arrowId, 'arrow')
      return {
        start: bs.find((b) => b.props.terminal === 'start')?.toId ?? null,
        end: bs.find((b) => b.props.terminal === 'end')?.toId ?? null,
      }
    },
    pageXY: (shape) => {
      const b = editor.getShapePageBounds(shape.id)
      return b ? { x: b.x, y: b.y } : { x: shape.x, y: shape.y }
    },
    childIds: (parentId) => editor.getSortedChildIdsForParent(parentId),
  }
}

export function installCollector(editor: Editor, opts: CollectorOptions): () => void {
  const pending = emptyPending()
  let opsTimer: ReturnType<typeof setTimeout> | null = null
  let layoutTimer: ReturnType<typeof setTimeout> | null = null
  let layoutAcc: LayoutPatch | null = null
  const vetoed = new Set<string>()
  let disposed = false

  const flushLayout = () => {
    layoutTimer = null
    if (!layoutAcc || disposed) return
    const patch = layoutAcc
    layoutAcc = null
    opts.onLayout(patch)
  }

  const flushOps = () => {
    opsTimer = null
    if (disposed) return
    const ctx = liveContext(editor, opts.getDiagram())
    const derived = deriveOps(ctx, pending)
    pending.added.clear()
    pending.updated.clear()
    pending.removed.clear()
    if (derived.stamps.length) {
      remote(editor, () => {
        for (const st of derived.stamps) {
          if (editor.getShape(st.id)) editor.updateShape({ id: st.id, type: st.type, meta: st.meta })
        }
      })
    }
    if (derived.ops.length) opts.onOps(derived.ops)
    if (derived.layout) {
      layoutAcc = mergeLayoutPatch(layoutAcc ?? {}, derived.layout)
      if (layoutTimer) clearTimeout(layoutTimer)
      layoutTimer = setTimeout(flushLayout, opts.layoutDebounceMs ?? 500)
    }
    if (vetoed.size) {
      // tldraw stripped the vetoed nodes' bindings before we could refuse; re-bind from truth
      const ids = new Set(vetoed)
      vetoed.clear()
      for (const e of opts.getEdges()) if (ids.has(e.src) || ids.has(e.dst)) upsertEdge(editor, e)
    }
  }

  const schedule = () => {
    if (disposed) return
    if (opsTimer) clearTimeout(opsTimer)
    opsTimer = setTimeout(flushOps, opts.opsDebounceMs ?? 250)
  }

  const offListen = editor.store.listen(
    (entry) => {
      mergeDiff(pending, entry.changes)
      schedule()
    },
    { source: 'user', scope: 'document' },
  )
  const offComplete = editor.sideEffects.registerOperationCompleteHandler((source) => {
    if (source === 'user' && (!isEmptyPending(pending) || vetoed.size)) schedule()
  })
  const offVeto = editor.sideEffects.registerBeforeDeleteHandler('shape', (shape, source) => {
    if (source !== 'user') return
    const kind = kindOf(shape)
    if (kind === 'plan-node') {
      // the server decides: send `deleted`, keep the shape until plan.node.delete arrives
      pending.removed.set(shape.id, shape)
      vetoed.add(String(shape.meta.planId))
      schedule()
      return false
    }
    if (kind === 'plan-frame' || kind === 'agent-card') return false
    return
  })

  return () => {
    disposed = true
    offListen()
    offComplete()
    offVeto()
    if (opsTimer) clearTimeout(opsTimer)
    if (layoutTimer) clearTimeout(layoutTimer)
  }
}
