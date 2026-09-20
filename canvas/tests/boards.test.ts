import { describe, expect, it } from 'vitest'
import type { TLFrameShape, TLShape, TLShapeId } from 'tldraw'
import {
  BOARDS,
  BOARD_STRIDE,
  boardAtPoint,
  boardBounds,
  boardFrameId,
  boardLayoutKey,
  boardNameFromKey,
  boardOf,
  boardOfFrameId,
  boardOfShape,
  boardSpecsFor,
  edgeScope,
  ensureBoards,
  isBoardFrameId,
  presentBoards,
  splitLayoutByBoard,
  type BoardSpec,
} from '../src/sync/boards'
import type { Editor } from 'tldraw'

// ---- a fake editor: boards.ts only ever reads/creates shapes by id ----

function fakeEditor(shapes: TLShape[] = []): Editor {
  const byId = new Map(shapes.map((s) => [String(s.id), s]))
  return {
    getShape: (id: TLShapeId) => byId.get(String(id)),
    createShape: (s: Partial<TLShape>) => byId.set(String(s.id), s as TLShape),
  } as unknown as Editor
}

function frameAt(name: string, x: number, y: number, w = 1600, h = 1000): TLShape {
  return { id: boardFrameId(name), type: 'frame', x, y, props: { w, h, name }, meta: { kind: 'plan-frame', planId: boardLayoutKey(name) } } as unknown as TLShape
}

// ---- tests ----

describe('board vocabulary', () => {
  it('lays the three boards out left to right with stable frame ids', () => {
    expect(BOARDS.map((b) => b.name)).toEqual(['hld', 'lld', 'er'])
    expect(BOARDS.map((b) => b.x)).toEqual([0, BOARD_STRIDE, BOARD_STRIDE * 2])
    expect(BOARDS.map((b) => b.title)).toEqual(['HLD · System', 'LLD · Components', 'ER · Data'])
    expect(boardFrameId('hld')).toBe('shape:f_board-hld')
    expect(boardFrameId('lld')).toBe('shape:f_board-lld')
    expect(boardFrameId('er')).toBe('shape:f_board-er')
    expect(boardLayoutKey('er')).toBe('board-er')
    expect(boardNameFromKey('board-er')).toBe('er')
    expect(boardNameFromKey('hld')).toBe('hld')
    expect(isBoardFrameId('shape:f_board-lld')).toBe(true)
    expect(isBoardFrameId('shape:f_plan-board')).toBe(false)
    expect(boardOfFrameId(boardFrameId('lld'))).toBe('lld')
    expect(boardOfFrameId('shape:n_node-a')).toBeNull()
  })

  it('creates the ER board only when the plan has an er diagram', () => {
    expect(boardSpecsFor(['hld']).map((b) => b.name)).toEqual(['hld', 'lld'])
    expect(boardSpecsFor(['hld', 'lld']).map((b) => b.name)).toEqual(['hld', 'lld'])
    expect(boardSpecsFor(['hld', 'er', 'api']).map((b) => b.name)).toEqual(['hld', 'lld', 'er'])
  })
})

describe('boardOf', () => {
  it('homes a node on its own type', () => {
    expect(boardOf({ type: 'hld' }, ['hld', 'lld', 'er'])).toBe('hld')
    expect(boardOf({ type: 'lld' }, ['hld', 'lld', 'er'])).toBe('lld')
    expect(boardOf({ type: 'er' }, ['hld', 'lld', 'er'])).toBe('er')
  })

  it('falls back to hld when that board is absent (A.0 home rule)', () => {
    // no ER frame: an `er` node is drawn on HLD rather than vanishing
    expect(boardOf({ type: 'er' }, ['hld', 'lld'])).toBe('hld')
    expect(boardOf({ type: 'lld' }, ['hld'])).toBe('hld')
    expect(boardOf({ type: 'api' }, ['hld', 'lld', 'er'])).toBe('hld')
    expect(boardOf({ type: null }, ['hld', 'lld', 'er'])).toBe('hld')
  })
})

describe('re-parenting on a type change', () => {
  const present = ['hld', 'lld', 'er']

  it('moves the node to the new board and its layout entry to that board’s sidecar', () => {
    const before = { id: 'node-parser', type: 'hld' }
    const after = { ...before, type: 'lld' }
    const oldFrame = boardFrameId(boardOf(before, present))
    const newFrame = boardFrameId(boardOf(after, present))
    expect(oldFrame).not.toBe(newFrame)
    // the old parent is a board frame, which is what arms the re-parent path in `upsertNode`
    expect(isBoardFrameId(oldFrame)).toBe(true)

    // the entry the re-parent hands to the wiring, keyed by the NEW board
    const patch = { nodes: { 'node-parser': { x: BOARD_STRIDE + 40, y: 40, w: 200, h: 64, parent: boardLayoutKey('lld'), pinned: true } } }
    const split = splitLayoutByBoard(patch, 'hld')
    expect([...split.keys()]).toEqual(['lld'])
    expect(split.get('lld')?.nodes?.['node-parser']).toMatchObject({ parent: 'board-lld', pinned: true })
  })

  it('keeps a node on hld when its new type has no board', () => {
    const before = { id: 'node-users', type: 'hld' }
    const after = { ...before, type: 'er' }
    expect(boardFrameId(boardOf(after, ['hld', 'lld']))).toBe(boardFrameId(boardOf(before, ['hld', 'lld'])))
  })

  it('splits one drag across the boards it touched, agents falling back to the active board', () => {
    const split = splitLayoutByBoard(
      {
        nodes: { 'node-a': { x: 1, y: 1, parent: 'board-hld' }, 'node-b': { x: 2, y: 2, parent: 'board-er' } },
        frames: { 'board-er': { x: 3600, y: 0, w: 1600, h: 1000 } },
        agents: { 'agent-x': { x: 9, y: 9 } },
      },
      'hld',
    )
    expect([...split.keys()].sort()).toEqual(['er', 'hld'])
    expect(split.get('hld')?.nodes).toEqual({ 'node-a': { x: 1, y: 1, parent: 'board-hld' } })
    expect(split.get('hld')?.agents).toEqual({ 'agent-x': { x: 9, y: 9 } })
    expect(split.get('er')?.frames).toEqual({ 'board-er': { x: 3600, y: 0, w: 1600, h: 1000 } })
  })
})

describe('cross-board edges', () => {
  it('classifies an edge by whether its endpoints share a board', () => {
    expect(edgeScope('hld', 'hld')).toBe('local')
    expect(edgeScope('hld', 'lld')).toBe('cross')
    expect(edgeScope('er', 'er')).toBe('local')
    // an endpoint that is not in any board frame reads as cross, so the arrow is page-parented
    expect(edgeScope('hld', null)).toBe('cross')
    expect(edgeScope(null, null)).toBe('cross')
  })

  it('reads an endpoint’s board off its parent frame', () => {
    const inHld = { parentId: boardFrameId('hld') } as unknown as TLShape
    const inLld = { parentId: boardFrameId('lld') } as unknown as TLShape
    const loose = { parentId: 'page:page' } as unknown as TLShape
    expect(boardOfShape(inHld)).toBe('hld')
    expect(edgeScope(boardOfShape(inHld), boardOfShape(inLld))).toBe('cross')
    expect(edgeScope(boardOfShape(inHld), boardOfShape(inHld))).toBe('local')
    expect(edgeScope(boardOfShape(inHld), boardOfShape(loose))).toBe('cross')
  })
})

describe('ensureBoards', () => {
  it('creates the missing frames once, titled and placed, and is idempotent', () => {
    const editor = fakeEditor()
    const specs = boardSpecsFor(['hld', 'er']) as BoardSpec[]
    const ids = ensureBoards(editor, specs)
    expect([...ids.keys()]).toEqual(['hld', 'lld', 'er'])
    expect(presentBoards(editor)).toEqual(['hld', 'lld', 'er'])
    const er = editor.getShape(boardFrameId('er')) as unknown as TLFrameShape
    expect(er.x).toBe(BOARD_STRIDE * 2)
    expect(er.props.name).toBe('ER · Data')
    expect(er.meta).toMatchObject({ kind: 'plan-frame', planId: 'board-er', board: 'er', collapsed: false })
    // second call creates nothing new
    let created = 0
    const counting = { ...editor, createShape: () => created++ } as unknown as Editor
    ensureBoards(counting, specs)
    expect(created).toBe(0)
  })

  it('honours a saved frame geometry from the sidecar', () => {
    const editor = fakeEditor()
    ensureBoards(editor, boardSpecsFor(['hld']), { 'board-hld': { x: 10, y: 20, w: 900, h: 500, collapsed: true } })
    const hld = editor.getShape(boardFrameId('hld')) as unknown as TLFrameShape
    expect({ x: hld.x, y: hld.y, w: hld.props.w }).toEqual({ x: 10, y: 20, w: 900 })
    expect(hld.props.h).toBe(48) // collapsed
    expect(hld.meta).toMatchObject({ collapsed: true, expandedH: 500 })
  })
})

describe('boardBounds / boardAtPoint', () => {
  const editor = fakeEditor([frameAt('hld', 0, 0), frameAt('lld', BOARD_STRIDE, 0)])

  it('gives a frame’s page bounds for the camera', () => {
    expect(boardBounds(editor, 'hld')).toMatchObject({ x: 0, y: 0, w: 1600, h: 1000 })
    expect(boardBounds(editor, 'er')).toBeNull()
  })

  it('names the board under the viewport centre (how panning sets activeBoard)', () => {
    expect(boardAtPoint(editor, { x: 800, y: 500 })).toBe('hld')
    expect(boardAtPoint(editor, { x: BOARD_STRIDE + 10, y: 10 })).toBe('lld')
    // in the gutter between the boards: nothing, so the switcher keeps its last reading
    expect(boardAtPoint(editor, { x: 1700, y: 500 })).toBeNull()
  })
})
