// Boards as framed regions (plan B.5). HLD, LLD and ER are three large titled frames on the one
// tldraw page; the board switcher moves the camera between them and `activeBoard` follows the
// camera back. Board membership is `node.type` (A.0's home rule) with `hld` as the fallback.
//
// Everything above the `----- editor -----` line is pure and covered by tests/boards.test.ts.
import { Box, type Editor, type TLDefaultColorStyle, type TLFrameShape, type TLShape, type TLShapeId } from 'tldraw'
import { frameShapeId, shapeMeta } from '../shapes/ids'
import type { LayoutFrame, LayoutPatch, NodeType } from '../state/types'

export type BoardName = NodeType

export const BOARD_W = 1600
export const BOARD_H = 1000
/** left-to-right spacing of the board origins: 1600 wide with a 200 gutter */
export const BOARD_STRIDE = 1800
/** `frames['board-hld']` in the layout sidecar, `shape:f_board-hld` on the canvas */
export const BOARD_FRAME_PREFIX = 'board-'
export const DEFAULT_BOARD: BoardName = 'hld'

export interface BoardSpec {
  name: BoardName
  /** the frame's title, shown in its header */
  title: string
  x: number
  y: number
  w: number
  h: number
  /** the tldraw colour nearest the board hue token (`--wb-hld` / `--wb-lld` / `--wb-er`) */
  color: TLDefaultColorStyle
}

export const BOARDS: readonly BoardSpec[] = [
  { name: 'hld', title: 'HLD · System', x: 0, y: 0, w: BOARD_W, h: BOARD_H, color: 'blue' },
  { name: 'lld', title: 'LLD · Components', x: BOARD_STRIDE, y: 0, w: BOARD_W, h: BOARD_H, color: 'violet' },
  { name: 'er', title: 'ER · Data', x: BOARD_STRIDE * 2, y: 0, w: BOARD_W, h: BOARD_H, color: 'green' },
]

export const BOARD_NAMES: readonly BoardName[] = BOARDS.map((b) => b.name)

export function isBoardName(name: string | null | undefined): name is BoardName {
  return name === 'hld' || name === 'lld' || name === 'er'
}

export function boardSpec(name: string): BoardSpec | undefined {
  return BOARDS.find((b) => b.name === name)
}

/** sidecar key / `meta.planId` of a board frame */
export function boardLayoutKey(name: string): string {
  return `${BOARD_FRAME_PREFIX}${name}`
}

/** `'board-hld'` → `'hld'`; anything else is returned unchanged */
export function boardNameFromKey(key: string): string {
  return key.startsWith(BOARD_FRAME_PREFIX) ? key.slice(BOARD_FRAME_PREFIX.length) : key
}

export function boardFrameId(name: string): TLShapeId {
  return frameShapeId(boardLayoutKey(name))
}

const BOARD_FRAME_IDS = new Set<string>(BOARDS.map((b) => boardFrameId(b.name)))

export function isBoardFrameId(id: string | null | undefined): boolean {
  return !!id && BOARD_FRAME_IDS.has(id)
}

/** the board a frame id belongs to, or null when it is not a board frame */
export function boardOfFrameId(id: string | null | undefined): BoardName | null {
  for (const b of BOARDS) if (id === boardFrameId(b.name)) return b.name
  return null
}

/**
 * Which boards exist for a snapshot: HLD and LLD always, ER only when the plan actually has an
 * `er` diagram (B.5 — no empty data board on projects without data).
 */
export function boardSpecsFor(diagramNames: Iterable<string>): BoardSpec[] {
  const names = new Set(diagramNames)
  return BOARDS.filter((b) => b.name !== 'er' || names.has('er'))
}

/** A.0's home rule: the node's own type when that board exists, else `hld`. */
export function boardOf(node: { type?: string | null }, present: Iterable<string>): BoardName {
  const type = node?.type
  if (!isBoardName(type)) return DEFAULT_BOARD
  for (const p of present) if (p === type) return type
  return DEFAULT_BOARD
}

/**
 * B.4: endpoints in different frames make a *cross-board* edge — parented to the page and drawn
 * at 1.5px / 40% opacity so it reads as secondary. Unplaced endpoints count as cross too.
 */
export function edgeScope(srcBoard: string | null | undefined, dstBoard: string | null | undefined): 'local' | 'cross' {
  return srcBoard && dstBoard && srcBoard === dstBoard ? 'local' : 'cross'
}

/**
 * Split one collected layout patch into per-board patches: node entries follow their `parent`
 * board key, frame entries follow their own key, and everything else lands on `fallback`
 * (the active board). Keys are board names, ready for `canvas.layout {diagram}`.
 */
export function splitLayoutByBoard(patch: LayoutPatch, fallback: string): Map<string, LayoutPatch> {
  const out = new Map<string, LayoutPatch>()
  const at = (board: string): LayoutPatch => {
    let p = out.get(board)
    if (!p) {
      p = {}
      out.set(board, p)
    }
    return p
  }
  for (const [id, entry] of Object.entries(patch.nodes ?? {})) {
    const board = entry.parent ? boardNameFromKey(entry.parent) : fallback
    const p = at(board)
    p.nodes = { ...(p.nodes ?? {}), [id]: entry }
  }
  for (const [key, entry] of Object.entries(patch.frames ?? {})) {
    const p = at(boardNameFromKey(key))
    p.frames = { ...(p.frames ?? {}), [key]: entry }
  }
  const rest: LayoutPatch = {}
  if (patch.agents && Object.keys(patch.agents).length) rest.agents = patch.agents
  if (patch.edges && Object.keys(patch.edges).length) rest.edges = patch.edges
  if (patch.direction) rest.direction = patch.direction
  if (Object.keys(rest).length) {
    const p = at(fallback)
    if (rest.agents) p.agents = { ...(p.agents ?? {}), ...rest.agents }
    if (rest.edges) p.edges = { ...(p.edges ?? {}), ...rest.edges }
    if (rest.direction) p.direction = rest.direction
  }
  return out
}

// ----- editor -----

/** the board frames that currently exist, in board order */
export function presentBoards(editor: Editor): BoardName[] {
  return BOARDS.filter((b) => !!editor.getShape(boardFrameId(b.name))).map((b) => b.name)
}

export function boardFrame(editor: Editor, name: string): TLFrameShape | undefined {
  return editor.getShape<TLFrameShape>(boardFrameId(name))
}

/** the board a shape lives on (by its parent frame), or null when it is not in a board */
export function boardOfShape(shape: TLShape | undefined): BoardName | null {
  return shape ? boardOfFrameId(String(shape.parentId)) : null
}

/**
 * Create the missing board frames. Idempotent; returns name → frame id for every spec.
 * `frames` supplies a saved geometry per board (the sidecar's `frames['board-<name>']`).
 */
export function ensureBoards(
  editor: Editor,
  boards: readonly BoardSpec[] = BOARDS,
  frames: Record<string, LayoutFrame | undefined> = {},
  write: (fn: () => void) => void = (fn) => fn(),
): Map<BoardName, TLShapeId> {
  const out = new Map<BoardName, TLShapeId>()
  const missing: BoardSpec[] = []
  for (const b of boards) {
    const id = boardFrameId(b.name)
    out.set(b.name, id)
    if (!editor.getShape(id)) missing.push(b)
  }
  if (missing.length) {
    write(() => {
      for (const b of missing) {
        const saved = frames[boardLayoutKey(b.name)]
        const f = { x: b.x, y: b.y, w: b.w, h: b.h, ...(saved ?? {}) }
        const collapsed = !!saved?.collapsed
        editor.createShape<TLFrameShape>({
          id: boardFrameId(b.name),
          type: 'frame',
          x: f.x,
          y: f.y,
          props: { w: f.w, h: collapsed ? COLLAPSED_BOARD_H : f.h, name: b.title, color: b.color },
          meta: shapeMeta('plan-frame', boardLayoutKey(b.name), { board: b.name, collapsed, expandedH: f.h }),
        })
      }
    })
  }
  return out
}

/** kept in step with `COLLAPSED_H` in apply.ts (imported there, declared here to avoid a cycle) */
export const COLLAPSED_BOARD_H = 48

/** page-space bounds of a board frame (what the switcher zooms to) */
export function boardBounds(editor: Editor, name: string): Box | null {
  const frame = boardFrame(editor, name)
  if (!frame) return null
  return new Box(frame.x, frame.y, frame.props.w, frame.props.h)
}

/** the board whose frame covers a page point — how panning updates `activeBoard` (B.5) */
export function boardAtPoint(editor: Editor, point: { x: number; y: number }): BoardName | null {
  for (const name of presentBoards(editor)) {
    const b = boardBounds(editor, name)
    if (b && point.x >= b.minX && point.x <= b.maxX && point.y >= b.minY && point.y <= b.maxY) return name
  }
  return null
}

// ----- re-parent notifications -----

/** `(board, patch)`: the moved node's layout entry, for the *new* board's sidecar */
export type ReparentListener = (board: string, patch: LayoutPatch) => void

const reparentListeners = new Set<ReparentListener>()

/** the wiring subscribes here so a re-homed node's sidecar entry follows it to the new board */
export function onBoardReparent(fn: ReparentListener): () => void {
  reparentListeners.add(fn)
  return () => reparentListeners.delete(fn)
}

export function notifyBoardReparent(board: string, patch: LayoutPatch): void {
  reparentListeners.forEach((l) => l(board, patch))
}
