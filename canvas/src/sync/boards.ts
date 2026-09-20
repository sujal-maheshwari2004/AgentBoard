// Boards as framed regions (plan B.5). HLD, LLD and ER are three large titled frames on the one
// tldraw page; the board switcher moves the camera between them and `activeBoard` follows the
// camera back. Board membership is `node.type` (A.0's home rule) with `hld` as the fallback.
//
// Everything above the `----- editor -----` line is pure and covered by tests/boards.test.ts.
import { Box, type Editor, type TLDefaultColorStyle, type TLFrameShape, type TLShape, type TLShapeId } from 'tldraw'
import { frameShapeId, shapeMeta } from '../shapes/ids'
import type { LayoutFrame, LayoutPatch, NodeType } from '../state/types'

export type BoardName = NodeType

/** the size a board frame starts at and never shrinks below (B.S6 item 2) */
export const BOARD_MIN_W = 720
export const BOARD_MIN_H = 480
/** legacy default geometry, kept for the sidecar entries written by B.S2 */
export const BOARD_W = 1600
export const BOARD_H = 1000
/** breathing room between a board's content and its frame edge */
export const BOARD_CONTENT_PAD = 48
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
  { name: 'hld', title: 'HLD · System', x: 0, y: 0, w: BOARD_MIN_W, h: BOARD_MIN_H, color: 'blue' },
  { name: 'lld', title: 'LLD · Components', x: BOARD_STRIDE, y: 0, w: BOARD_MIN_W, h: BOARD_MIN_H, color: 'violet' },
  { name: 'er', title: 'ER · Data', x: BOARD_STRIDE * 2, y: 0, w: BOARD_MIN_W, h: BOARD_MIN_H, color: 'green' },
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

// ----- content bounds (B.S6 item 2) -----

export interface BoxLike {
  x: number
  y: number
  w: number
  h: number
}

/**
 * The size a frame needs for its children: the content extent plus `pad` on every side, never
 * below (`minW`, `minH`). Children are in FRAME-LOCAL coordinates, so the origin stays at (0,0)
 * and only the extent matters — the frame is anchored, not re-centred.
 *
 * `extra` is added to each child's height: a plan node's label hangs *below* its box (B.3), so
 * the frame has to reserve that strip or the bottom row's titles fall outside the board.
 */
export function contentSize(
  children: readonly BoxLike[],
  opts: { pad?: number; minW?: number; minH?: number; extra?: number } = {},
): { w: number; h: number } {
  const pad = opts.pad ?? BOARD_CONTENT_PAD
  const minW = opts.minW ?? BOARD_MIN_W
  const minH = opts.minH ?? BOARD_MIN_H
  const extra = opts.extra ?? 0
  let w = 0
  let h = 0
  for (const c of children) {
    w = Math.max(w, c.x + c.w)
    h = Math.max(h, c.y + c.h + extra)
  }
  if (w === 0 && h === 0) return { w: minW, h: minH }
  return { w: Math.max(minW, Math.round(w + pad)), h: Math.max(minH, Math.round(h + pad)) }
}

/**
 * The box the camera should frame on load (B.S6 item 2): the board's actual content, padded,
 * rather than the whole frame — an empty-looking board is a board whose camera was fitted to a
 * 1600x1000 rectangle holding six small boxes. Falls back to the frame when it has no content.
 */
export function contentViewBox(
  frame: BoxLike,
  children: readonly BoxLike[],
  opts: { pad?: number; extra?: number; header?: number } = {},
): BoxLike {
  const pad = opts.pad ?? BOARD_CONTENT_PAD
  const extra = opts.extra ?? 0
  // the board's own title and disclosure button are drawn ABOVE the frame edge, so the view box
  // reaches that far up: landing on a board whose name is off screen reads as "which board?"
  const header = opts.header ?? 0
  if (children.length === 0) return { ...frame }
  let minX = Infinity
  let minY = Infinity
  let maxX = -Infinity
  let maxY = -Infinity
  for (const c of children) {
    minX = Math.min(minX, c.x)
    minY = Math.min(minY, c.y)
    maxX = Math.max(maxX, c.x + c.w)
    maxY = Math.max(maxY, c.y + c.h + extra)
  }
  // frame-local children -> page coords, then pad, clamped so we never frame outside the board
  const x = frame.x + Math.max(0, minX - pad)
  const y = frame.y + Math.max(0, minY - pad) - header
  const w = Math.min(frame.w, maxX - minX + pad * 2)
  const h = Math.min(frame.h + header, maxY - minY + pad * 2 + header)
  return { x, y, w: Math.max(1, w), h: Math.max(1, h) }
}

/**
 * Which board the switcher should light up for a viewport (B.5 / B.S6 item 14). The viewport
 * centre wins when it is inside a board; zoomed far out the centre usually lands in the gutter
 * between two boards, so the board with the largest visible area wins instead. `null` only when
 * no board is on screen at all — the caller then leaves `activeBoard` alone.
 */
export function boardForViewport(
  boards: readonly { name: BoardName; box: BoxLike }[],
  viewport: BoxLike,
): BoardName | null {
  const cx = viewport.x + viewport.w / 2
  const cy = viewport.y + viewport.h / 2
  for (const b of boards) {
    if (cx >= b.box.x && cx <= b.box.x + b.box.w && cy >= b.box.y && cy <= b.box.y + b.box.h) return b.name
  }
  let best: BoardName | null = null
  let bestArea = 0
  for (const b of boards) {
    const ow = Math.min(viewport.x + viewport.w, b.box.x + b.box.w) - Math.max(viewport.x, b.box.x)
    const oh = Math.min(viewport.y + viewport.h, b.box.y + b.box.h) - Math.max(viewport.y, b.box.y)
    const area = ow > 0 && oh > 0 ? ow * oh : 0
    if (area > bestArea) {
      bestArea = area
      best = b.name
    }
  }
  return best
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
