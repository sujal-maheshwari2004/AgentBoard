// dagre auto-layout. dagre returns CENTRES; tldraw wants TOP-LEFT (converted here).
// Runs only for nodes without a saved position or on an explicit Re-layout; pinned nodes never move.
import dagre from '@dagrejs/dagre'

export interface LayoutNodeInput { id: string; w: number; h: number }
export interface LayoutEdgeInput { from: string; to: string }
export interface Position { x: number; y: number }
export type Positions = Record<string, Position>

export const NODE_W = 200
export const NODE_H = 80
export const FRAME_PAD = 40

export function rankdir(direction: string | undefined): 'TB' | 'LR' | 'RL' | 'BT' {
  switch ((direction ?? 'TD').toUpperCase()) {
    case 'LR':
      return 'LR'
    case 'RL':
      return 'RL'
    case 'BT':
      return 'BT'
    default:
      return 'TB'
  }
}

/** Full dagre layout. Returns top-left positions relative to (0,0) with FRAME_PAD margins. */
export function layoutGraph(nodes: LayoutNodeInput[], edges: LayoutEdgeInput[], direction?: string): Positions {
  const g = new dagre.graphlib.Graph({ multigraph: true })
  g.setGraph({ rankdir: rankdir(direction), nodesep: 60, ranksep: 90, marginx: FRAME_PAD, marginy: FRAME_PAD })
  g.setDefaultEdgeLabel(() => ({}))
  const ids = new Set(nodes.map((n) => n.id))
  for (const n of nodes) g.setNode(n.id, { width: n.w, height: n.h })
  for (const e of edges) {
    if (!ids.has(e.from) || !ids.has(e.to)) continue
    g.setEdge(e.from, e.to, {}, `${e.from}->${e.to}`)
  }
  dagre.layout(g)
  const out: Positions = {}
  for (const n of nodes) {
    const p = g.node(n.id)
    if (!p) continue
    out[n.id] = { x: p.x - n.w / 2, y: p.y - n.h / 2 }
  }
  return out
}

export interface ExistingPosition extends Position { pinned?: boolean }

/**
 * Positions for nodes that have no saved position. Nodes in `existing` are returned unchanged.
 * A dagre layout of the whole graph is computed once; new nodes take their dagre slot, shifted
 * below any existing content so they never land on top of pinned boxes.
 */
export function positionsForMissing(
  nodes: LayoutNodeInput[],
  edges: LayoutEdgeInput[],
  existing: Record<string, ExistingPosition>,
  direction?: string,
): Positions {
  const missing = nodes.filter((n) => !existing[n.id])
  const out: Positions = {}
  for (const n of nodes) if (existing[n.id]) out[n.id] = { x: existing[n.id].x, y: existing[n.id].y }
  if (missing.length === 0) return out
  const laid = layoutGraph(nodes, edges, direction)
  const existingCount = nodes.length - missing.length
  if (existingCount === 0) {
    for (const n of missing) out[n.id] = laid[n.id]
    return out
  }
  // shift newcomers below the current content
  let maxY = -Infinity
  for (const n of nodes) {
    const p = existing[n.id]
    if (p) maxY = Math.max(maxY, p.y + n.h)
  }
  let minLaidY = Infinity
  for (const n of missing) minLaidY = Math.min(minLaidY, laid[n.id].y)
  const dy = maxY + FRAME_PAD - minLaidY
  for (const n of missing) out[n.id] = { x: laid[n.id].x, y: laid[n.id].y + dy }
  return out
}

/** Re-layout: unpinned nodes get fresh dagre slots; pinned nodes are untouched. */
export function relayoutUnpinned(
  nodes: LayoutNodeInput[],
  edges: LayoutEdgeInput[],
  existing: Record<string, ExistingPosition>,
  direction?: string,
): Positions {
  const laid = layoutGraph(nodes, edges, direction)
  const out: Positions = {}
  for (const n of nodes) {
    const p = existing[n.id]
    out[n.id] = p?.pinned ? { x: p.x, y: p.y } : laid[n.id]
  }
  return out
}

export function boundsOf(nodes: LayoutNodeInput[], positions: Positions): { w: number; h: number } {
  let maxX = 0
  let maxY = 0
  for (const n of nodes) {
    const p = positions[n.id]
    if (!p) continue
    maxX = Math.max(maxX, p.x + n.w)
    maxY = Math.max(maxY, p.y + n.h)
  }
  return { w: maxX + FRAME_PAD, h: maxY + FRAME_PAD }
}
