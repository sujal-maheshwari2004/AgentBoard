import { describe, expect, it } from 'vitest'
import { toRichText, type TLArrowBinding, type TLArrowShape, type TLGeoShape, type TLRecord, type TLRichText, type TLShape, type TLShapeId } from 'tldraw'
import { deriveOps, emptyPending, frameRefOf, mergeDiff, mergeLayoutPatch, type CollectContext, type Pending } from '../src/sync/collect'
import { bindingId, edgeShapeId, nodeShapeId, shapeMeta } from '../src/shapes/ids'
import { boardFrameId } from '../src/sync/boards'

// ---- synthetic records ----

// B.5: nodes live in board frames now; `board-hld` is the HLD board's sidecar key / planId
const FRAME = boardFrameId('hld')
const LLD_FRAME = boardFrameId('lld')
/** an agent folder (B.S3) — a frame we must NOT promote drawn boxes inside (B.12) */
const FOLDER = 'shape:g_agent-parser' as TLShape['parentId']
const PAGE = 'page:page' as TLShape['parentId']

function plaintext(rt: TLRichText): string {
  const out: string[] = []
  const walk = (n: unknown) => {
    if (!n || typeof n !== 'object') return
    const o = n as { type?: string; text?: string; content?: unknown[] }
    if (o.type === 'text' && typeof o.text === 'string') out.push(o.text)
    if (o.type === 'paragraph' && out.length) out.push('\n')
    o.content?.forEach(walk)
  }
  walk(rt)
  return out.join('').trim()
}

function geo(id: TLShapeId, opts: { label: string; x?: number; y?: number; w?: number; h?: number; parentId?: TLShape['parentId']; meta?: Record<string, unknown>; geo?: string }): TLGeoShape {
  return {
    id,
    typeName: 'shape',
    type: 'geo',
    x: opts.x ?? 10,
    y: opts.y ?? 20,
    rotation: 0,
    index: 'a1',
    parentId: opts.parentId ?? FRAME,
    isLocked: false,
    opacity: 1,
    meta: opts.meta ?? {},
    props: { geo: opts.geo ?? 'rectangle', w: opts.w ?? 200, h: opts.h ?? 80, richText: toRichText(opts.label) },
  } as unknown as TLGeoShape
}

function planNode(nodeId: string, label: string, extra: Partial<Parameters<typeof geo>[1]> = {}): TLGeoShape {
  return geo(nodeShapeId(nodeId), { label, meta: shapeMeta('plan-node', nodeId), ...extra })
}

function arrow(id: TLShapeId, opts: { label?: string; meta?: Record<string, unknown> } = {}): TLArrowShape {
  return {
    id,
    typeName: 'shape',
    type: 'arrow',
    x: 0,
    y: 0,
    rotation: 0,
    index: 'a2',
    parentId: FRAME,
    isLocked: false,
    opacity: 1,
    meta: opts.meta ?? {},
    props: { kind: 'elbow', start: { x: 0, y: 0 }, end: { x: 10, y: 10 }, richText: toRichText(opts.label ?? '') },
  } as unknown as TLArrowShape
}

function binding(arrowId: TLShapeId, toId: TLShapeId, terminal: 'start' | 'end'): TLArrowBinding {
  return {
    id: bindingId('x', 'y', terminal),
    typeName: 'binding',
    type: 'arrow',
    fromId: arrowId,
    toId,
    meta: {},
    props: { terminal, normalizedAnchor: { x: 0.5, y: 0.5 }, isExact: false, isPrecise: false, snap: 'none' },
  } as unknown as TLArrowBinding
}

interface World {
  shapes: Map<string, TLShape>
  ends: Map<string, { start: TLShapeId | null; end: TLShapeId | null }>
}

function ctxFor(world: World, frame = { x: 100, y: 200 }): CollectContext {
  return {
    frameKindOf: (s) => {
      if (s.parentId === FRAME) return { kind: 'board', id: 'board-hld' }
      if (s.parentId === LLD_FRAME) return { kind: 'board', id: 'board-lld' }
      if (s.parentId === FOLDER) return { kind: 'agent-folder', id: 'agent-parser' }
      return null
    },
    diagram: 'hld',
    plaintext,
    getShape: (id) => world.shapes.get(id),
    getArrowEnds: (id) => world.ends.get(id) ?? { start: null, end: null },
    pageXY: (s) => (s.parentId === FRAME ? { x: s.x + frame.x, y: s.y + frame.y } : { x: s.x, y: s.y }),
    childIds: (parent) => [...world.shapes.values()].filter((s) => s.parentId === parent).map((s) => s.id),
  }
}

function world(...shapes: TLShape[]): World {
  return { shapes: new Map(shapes.map((s) => [s.id, s])), ends: new Map() }
}

function pendingOf(p: { added?: TLRecord[]; updated?: Array<[TLRecord, TLRecord]>; removed?: TLRecord[] }): Pending {
  const out = emptyPending()
  for (const r of p.added ?? []) out.added.set(r.id, r)
  for (const [a, b] of p.updated ?? []) out.updated.set(b.id, [a, b])
  for (const r of p.removed ?? []) out.removed.set(r.id, r)
  return out
}

// ---- tests ----

describe('deriveOps', () => {
  it('emits renamed when a plan node label changes (plaintext compare)', () => {
    const before = planNode('node-parser', 'Mermaid parser')
    const after = planNode('node-parser', 'Mermaid parser v2')
    const w = world(after)
    const d = deriveOps(ctxFor(w), pendingOf({ updated: [[before, after]] }))
    expect(d.ops).toEqual([{ op: 'renamed', id: 'node-parser', label: 'Mermaid parser v2' }])
    expect(d.layout).toBeNull()
  })

  it('ignores richText-only changes with identical plaintext', () => {
    const before = planNode('node-a', 'Same')
    const after = { ...before, props: { ...before.props, richText: { ...toRichText('Same'), attrs: { bold: true } } } } as unknown as TLGeoShape
    const d = deriveOps(ctxFor(world(after)), pendingOf({ updated: [[before, after]] }))
    expect(d.ops).toEqual([])
  })

  it('turns a removed plan node into a deleted op', () => {
    const s = planNode('node-cli', 'CLI')
    const d = deriveOps(ctxFor(world()), pendingOf({ removed: [s] }))
    expect(d.ops).toEqual([{ op: 'deleted', id: 'node-cli' }])
  })

  it('turns a removed edge arrow into edge-deleted', () => {
    const a = arrow(edgeShapeId('node-a', 'node-b'), { meta: shapeMeta('edge', 'node-a__node-b', { diagram: 'hld' }) })
    const d = deriveOps(ctxFor(world()), pendingOf({ removed: [a] }))
    expect(d.ops).toEqual([{ op: 'edge-deleted', from: 'node-a', to: 'node-b', diagram: 'hld' }])
  })

  it('creates a provisional node for a human-drawn labelled box inside the frame', () => {
    const s = geo('shape:human1' as TLShapeId, { label: 'Auth service', x: 30, y: 40 })
    const d = deriveOps(ctxFor(world(s)), pendingOf({ added: [s] }))
    expect(d.ops).toEqual([{ op: 'node-created', id: 'node-auth-service', label: 'Auth service', shape: 'rect', diagram: 'hld' }])
    expect(d.stamps).toEqual([{ id: s.id, type: 'geo', meta: { kind: 'plan-node', planId: 'node-auth-service', provisional: true } }])
    expect(d.layout?.nodes?.['node-auth-service']).toMatchObject({ x: 130, y: 240, w: 200, h: 80, parent: 'board-hld', pinned: true })
  })

  it('stamps the board it was drawn on, not the active board', () => {
    const s = geo('shape:human2' as TLShapeId, { label: 'Token store', parentId: LLD_FRAME })
    const d = deriveOps(ctxFor(world(s)), pendingOf({ added: [s] }))
    expect(d.ops).toEqual([{ op: 'node-created', id: 'node-token-store', label: 'Token store', shape: 'rect', diagram: 'lld' }])
    expect(d.layout?.nodes?.['node-token-store']).toMatchObject({ parent: 'board-lld', pinned: true })
  })

  it('yields no op for a box drawn outside any board frame (page or agent folder)', () => {
    const loose = geo('shape:h4' as TLShapeId, { label: 'Note to self', parentId: PAGE })
    expect(deriveOps(ctxFor(world(loose)), pendingOf({ added: [loose] }))).toMatchObject({ ops: [], stamps: [], layout: null })
    const inFolder = geo('shape:h5' as TLShapeId, { label: 'Parser class', parentId: FOLDER })
    expect(deriveOps(ctxFor(world(inFolder)), pendingOf({ added: [inFolder] }))).toMatchObject({ ops: [], stamps: [], layout: null })
  })

  it('waits for a label before creating a node, and ignores boxes outside the frame', () => {
    const empty = geo('shape:h2' as TLShapeId, { label: '' })
    expect(deriveOps(ctxFor(world(empty)), pendingOf({ added: [empty] })).ops).toEqual([])
    const outside = geo('shape:h3' as TLShapeId, { label: 'Note to self', parentId: PAGE })
    expect(deriveOps(ctxFor(world(outside)), pendingOf({ added: [outside] })).ops).toEqual([])
    // labelled later → created on the update
    const labelled = geo('shape:h2' as TLShapeId, { label: 'Cache', geo: 'ellipse' })
    const d = deriveOps(ctxFor(world(labelled)), pendingOf({ updated: [[empty, labelled]] }))
    expect(d.ops).toEqual([{ op: 'node-created', id: 'node-cache', label: 'Cache', shape: 'round', diagram: 'hld' }])
  })

  it('resolves a human arrow bound at both ends to edge-created', () => {
    const a = planNode('node-a', 'A')
    const b = planNode('node-b', 'B')
    const ar = arrow('shape:arrow1' as TLShapeId, { label: 'uses' })
    const w = world(a, b, ar)
    w.ends.set(ar.id, { start: a.id, end: b.id })
    const d = deriveOps(ctxFor(w), pendingOf({ added: [ar, binding(ar.id, a.id, 'start'), binding(ar.id, b.id, 'end')] }))
    expect(d.ops).toEqual([{ op: 'edge-created', from: 'node-a', to: 'node-b', label: 'uses', diagram: 'hld' }])
    expect(d.stamps).toEqual([{ id: ar.id, type: 'arrow', meta: { kind: 'edge', planId: 'node-a__node-b', diagram: 'hld', provisional: true } }])
  })

  it('ignores dangling arrows until both ends bind', () => {
    const a = planNode('node-a', 'A')
    const ar = arrow('shape:arrow2' as TLShapeId)
    const w = world(a, ar)
    w.ends.set(ar.id, { start: a.id, end: null })
    const d = deriveOps(ctxFor(w), pendingOf({ added: [ar, binding(ar.id, a.id, 'start')] }))
    expect(d.ops).toEqual([])
    expect(d.stamps).toEqual([])
  })

  it('ignores arrows bound to non-plan shapes (agent cards, freehand) and self loops', () => {
    const a = planNode('node-a', 'A')
    const card = { ...geo('shape:a_agent-x' as TLShapeId, { label: '', parentId: PAGE }), type: 'agent-card', meta: shapeMeta('agent-card', 'agent-x') } as unknown as TLShape
    const ar = arrow('shape:arrow3' as TLShapeId)
    const w = world(a, card, ar)
    w.ends.set(ar.id, { start: a.id, end: card.id })
    expect(deriveOps(ctxFor(w), pendingOf({ added: [ar] })).ops).toEqual([])
    w.ends.set(ar.id, { start: a.id, end: a.id })
    expect(deriveOps(ctxFor(w), pendingOf({ added: [ar] })).ops).toEqual([])
  })

  it('emits edge-rerouted when an existing edge arrow is rebound (binding remove + add)', () => {
    const a = planNode('node-a', 'A')
    const b = planNode('node-b', 'B')
    const c = planNode('node-c', 'C')
    const ar = arrow(edgeShapeId('node-a', 'node-b'), { meta: shapeMeta('edge', 'node-a__node-b', { diagram: 'hld' }) })
    const w = world(a, b, c, ar)
    w.ends.set(ar.id, { start: a.id, end: c.id })
    const oldEnd = binding(ar.id, b.id, 'end')
    const newEnd = binding(ar.id, c.id, 'end')
    const d = deriveOps(ctxFor(w), pendingOf({ removed: [oldEnd], added: [newEnd] }))
    expect(d.ops).toEqual([{ op: 'edge-rerouted', from: 'node-a', to: 'node-b', new_from: 'node-a', new_to: 'node-c', diagram: 'hld' }])
    expect(d.stamps[0].meta).toMatchObject({ kind: 'edge', planId: 'node-a__node-c' })
  })

  it('emits nothing when an edge arrow is rebound to the same ends', () => {
    const a = planNode('node-a', 'A')
    const b = planNode('node-b', 'B')
    const ar = arrow(edgeShapeId('node-a', 'node-b'), { meta: shapeMeta('edge', 'node-a__node-b') })
    const w = world(a, b, ar)
    w.ends.set(ar.id, { start: a.id, end: b.id })
    const d = deriveOps(ctxFor(w), pendingOf({ updated: [[ar, ar]] }))
    expect(d.ops).toEqual([])
  })

  it('records cosmetic moves as a pinned layout patch in page coordinates', () => {
    const before = planNode('node-a', 'A', { x: 10, y: 20 })
    const after = planNode('node-a', 'A', { x: 50, y: 60, w: 220 })
    const d = deriveOps(ctxFor(world(after)), pendingOf({ updated: [[before, after]] }))
    expect(d.ops).toEqual([])
    expect(d.layout).toEqual({
      nodes: { 'node-a': { x: 150, y: 260, w: 220, h: 80, parent: 'board-hld', pinned: true } },
      agents: {},
      frames: {},
    })
  })

  it('records agent card moves and frame moves (with children re-paged)', () => {
    const cardBefore = { ...geo('shape:a_agent-x' as TLShapeId, { label: '', parentId: PAGE, x: 1300, y: 40, w: 240, h: 120 }), type: 'agent-card', meta: shapeMeta('agent-card', 'agent-x') } as unknown as TLShape
    const cardAfter = { ...cardBefore, x: 1400 } as TLShape
    const frameBefore = { ...geo(FRAME, { label: '', parentId: PAGE, x: 0, y: 0, w: 1000, h: 700 }), type: 'frame', meta: shapeMeta('plan-frame', 'board-hld', { collapsed: false }) } as unknown as TLShape
    const frameAfter = { ...frameBefore, x: 100, y: 200 } as TLShape
    const child = planNode('node-a', 'A', { x: 10, y: 20 })
    const d = deriveOps(ctxFor(world(cardAfter, frameAfter, child)), pendingOf({ updated: [[cardBefore, cardAfter], [frameBefore, frameAfter]] }))
    expect(d.ops).toEqual([])
    expect(d.layout?.agents).toEqual({ 'agent-x': { x: 1400, y: 40, w: 240, h: 120 } })
    expect(d.layout?.frames).toEqual({ 'board-hld': { x: 100, y: 200, w: 1000, h: 700, collapsed: false } })
    expect(d.layout?.nodes).toEqual({ 'node-a': { x: 110, y: 220, w: 200, h: 80, parent: 'board-hld', pinned: true } })
  })
})

describe('frameRefOf', () => {
  it('reads the frame kind off the parent shape meta', () => {
    const board = { meta: shapeMeta('plan-frame', 'board-lld') } as unknown as TLShape
    expect(frameRefOf(board)).toEqual({ kind: 'board', id: 'board-lld' })
    const folder = { meta: { kind: 'agent-folder', planId: 'agent-parser' } } as unknown as TLShape
    expect(frameRefOf(folder)).toEqual({ kind: 'agent-folder', id: 'agent-parser' })
    expect(frameRefOf(undefined)).toBeNull()
    expect(frameRefOf({ meta: {} } as unknown as TLShape)).toBeNull()
  })
})

describe('mergeDiff', () => {
  it('collapses added+updated into added and added+removed into nothing', () => {
    const p = emptyPending()
    const a = planNode('node-a', 'A')
    const a2 = planNode('node-a', 'A2')
    mergeDiff(p, { added: { [a.id]: a }, updated: {}, removed: {} })
    mergeDiff(p, { added: {}, updated: { [a.id]: [a, a2] }, removed: {} })
    expect(p.added.get(a.id)).toBe(a2)
    expect(p.updated.size).toBe(0)
    mergeDiff(p, { added: {}, updated: {}, removed: { [a.id]: a2 } })
    expect(p.added.size).toBe(0)
    expect(p.removed.size).toBe(0)
  })

  it('keeps the first `from` and last `to` across successive updates', () => {
    const p = emptyPending()
    const v1 = planNode('node-a', 'v1')
    const v2 = planNode('node-a', 'v2')
    const v3 = planNode('node-a', 'v3')
    mergeDiff(p, { added: {}, updated: { [v1.id]: [v1, v2] }, removed: {} })
    mergeDiff(p, { added: {}, updated: { [v1.id]: [v2, v3] }, removed: {} })
    expect(p.updated.get(v1.id)).toEqual([v1, v3])
    const d = deriveOps(ctxFor(world(v3)), p)
    expect(d.ops).toEqual([{ op: 'renamed', id: 'node-a', label: 'v3' }])
  })
})

describe('mergeLayoutPatch', () => {
  it('deep-merges the four sections', () => {
    const m = mergeLayoutPatch({ nodes: { a: { x: 1, y: 1 } } }, { nodes: { b: { x: 2, y: 2 } }, frames: { 'board-hld': { x: 0, y: 0, w: 1, h: 1 } } })
    expect(m.nodes).toEqual({ a: { x: 1, y: 1 }, b: { x: 2, y: 2 } })
    expect(m.frames).toEqual({ 'board-hld': { x: 0, y: 0, w: 1, h: 1 } })
  })
})
