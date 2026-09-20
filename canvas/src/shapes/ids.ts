// Stable tldraw ids derived from plan ids (research doc: "Stable ids").
//   n_<nodeId>          plan node (geo)
//   a_<agentId>         agent card (custom shape)
//   f_<name>            frame (board frames: `f_board-hld`)
//   g_<agentId>         agent folder (B.6 — a new prefix; `f_`'s parse is pinned by ids.test.ts)
//   e_<src>__<dst>      edge arrow
//   b_<src>__<dst>_<terminal>  arrow binding
// Plan ids match ^(node|agent)-[a-z0-9][a-z0-9-]*$ so `__` and `_` are safe separators.
import { createBindingId, createShapeId, type JsonObject, type TLBindingId, type TLShapeId } from 'tldraw'

export type ShapeKind = 'plan-node' | 'agent-card' | 'plan-frame' | 'edge' | 'agent-folder'

export const PLAN_FRAME_NAME = 'plan-board'
export const PLAN_FRAME_TITLE = 'Plan board'

export function nodeShapeId(id: string): TLShapeId {
  return createShapeId(`n_${id}`)
}
export function agentShapeId(id: string): TLShapeId {
  return createShapeId(`a_${id}`)
}
export function frameShapeId(name: string = PLAN_FRAME_NAME): TLShapeId {
  return createShapeId(`f_${name}`)
}
/** B.6 agent folder: `shape:g_agent-parser`. Deliberately NOT `f_`. */
export function folderShapeId(agentId: string): TLShapeId {
  return createShapeId(`g_${agentId}`)
}
export function edgeShapeId(src: string, dst: string): TLShapeId {
  return createShapeId(`e_${src}__${dst}`)
}
export function bindingId(src: string, dst: string, terminal: 'start' | 'end'): TLBindingId {
  return createBindingId(`b_${src}__${dst}_${terminal}`)
}

export interface ParsedShapeId {
  kind: ShapeKind
  /** plan id: node id, agent id, frame name, or `src__dst` for edges */
  id: string
  src?: string
  dst?: string
}

export function parseEdgeKey(key: string): { src: string; dst: string } | null {
  const i = key.indexOf('__')
  if (i <= 0 || i + 2 >= key.length) return null
  return { src: key.slice(0, i), dst: key.slice(i + 2) }
}

export function parseShapeId(shapeId: string): ParsedShapeId | null {
  if (!shapeId.startsWith('shape:')) return null
  const rest = shapeId.slice('shape:'.length)
  if (rest.length < 3 || rest[1] !== '_') return null
  const prefix = rest[0]
  const id = rest.slice(2)
  switch (prefix) {
    case 'n':
      return { kind: 'plan-node', id }
    case 'a':
      return { kind: 'agent-card', id }
    case 'f':
      return { kind: 'plan-frame', id }
    case 'g':
      return { kind: 'agent-folder', id }
    case 'e': {
      const pair = parseEdgeKey(id)
      if (!pair) return null
      return { kind: 'edge', id, src: pair.src, dst: pair.dst }
    }
    default:
      return null
  }
}

/** meta stamped on every shape we create */
export function shapeMeta(kind: ShapeKind, planId: string, extra: JsonObject = {}): JsonObject {
  return { kind, planId, ...extra }
}

export function slugify(label: string): string {
  const slug = label
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .replace(/-{2,}/g, '-')
  return slug || 'untitled'
}

/** provisional id for a node the human drew: node-<slug-of-label> */
export function provisionalNodeId(label: string): string {
  return `node-${slugify(label)}`
}
