import { describe, expect, it } from 'vitest'
import { FRAME_PAD, boundsOf, layoutGraph, positionsForMissing, rankdir, relayoutUnpinned } from '../src/layout/dagre'

const nodes = [
  { id: 'a', w: 200, h: 80 },
  { id: 'b', w: 200, h: 80 },
  { id: 'c', w: 200, h: 80 },
]
const edges = [
  { from: 'a', to: 'b' },
  { from: 'a', to: 'c' },
]

describe('dagre layout', () => {
  it('maps directions to rankdir', () => {
    expect(rankdir('TD')).toBe('TB')
    expect(rankdir('TB')).toBe('TB')
    expect(rankdir('lr')).toBe('LR')
    expect(rankdir('RL')).toBe('RL')
    expect(rankdir('BT')).toBe('BT')
    expect(rankdir(undefined)).toBe('TB')
  })

  it('converts dagre centres to top-left positions', () => {
    const pos = layoutGraph(nodes, edges, 'TD')
    // every box is fully inside the margin
    for (const n of nodes) {
      expect(pos[n.id].x).toBeGreaterThanOrEqual(FRAME_PAD - 0.01)
      expect(pos[n.id].y).toBeGreaterThanOrEqual(FRAME_PAD - 0.01)
    }
    // TB: a (root) is above b and c; a's centre lies between b and c's centres
    expect(pos.a.y).toBeLessThan(pos.b.y)
    expect(pos.a.y).toBeLessThan(pos.c.y)
    expect(pos.b.y).toBeCloseTo(pos.c.y)
    const ax = pos.a.x + 100
    const bx = pos.b.x + 100
    const cx = pos.c.x + 100
    expect(Math.min(bx, cx)).toBeLessThanOrEqual(ax + 0.01)
    expect(Math.max(bx, cx)).toBeGreaterThanOrEqual(ax - 0.01)
    // the first rank sits exactly at the margin (centre - h/2 == marginy)
    expect(pos.a.y).toBeCloseTo(FRAME_PAD)
  })

  it('lays out left-to-right when asked', () => {
    const pos = layoutGraph(nodes, edges, 'LR')
    expect(pos.a.x).toBeLessThan(pos.b.x)
    expect(pos.a.x).toBeLessThan(pos.c.x)
    expect(pos.b.x).toBeCloseTo(pos.c.x)
  })

  it('ignores edges to unknown nodes', () => {
    const pos = layoutGraph(nodes, [...edges, { from: 'a', to: 'zzz' }], 'TD')
    expect(Object.keys(pos).sort()).toEqual(['a', 'b', 'c'])
  })
})

describe('positionsForMissing', () => {
  it('returns existing positions unchanged and only lays out newcomers', () => {
    const existing = { a: { x: 5, y: 7, pinned: true }, b: { x: 300, y: 400 } }
    const pos = positionsForMissing(nodes, edges, existing, 'TD')
    expect(pos.a).toEqual({ x: 5, y: 7 })
    expect(pos.b).toEqual({ x: 300, y: 400 })
    expect(pos.c).toBeDefined()
    // newcomers are shifted below existing content
    expect(pos.c.y).toBeGreaterThanOrEqual(400 + 80 + FRAME_PAD - 0.01)
  })

  it('uses plain dagre when nothing is placed yet', () => {
    const pos = positionsForMissing(nodes, edges, {}, 'TD')
    expect(pos).toEqual(layoutGraph(nodes, edges, 'TD'))
  })

  it('is a no-op when everything is placed', () => {
    const existing = { a: { x: 1, y: 2 }, b: { x: 3, y: 4 }, c: { x: 5, y: 6 } }
    expect(positionsForMissing(nodes, edges, existing)).toEqual({ a: { x: 1, y: 2 }, b: { x: 3, y: 4 }, c: { x: 5, y: 6 } })
  })
})

describe('relayoutUnpinned', () => {
  it('moves unpinned nodes and leaves pinned nodes untouched', () => {
    const existing = { a: { x: 999, y: 999, pinned: true }, b: { x: 1, y: 1, pinned: false }, c: { x: 2, y: 2 } }
    const pos = relayoutUnpinned(nodes, edges, existing, 'TD')
    const fresh = layoutGraph(nodes, edges, 'TD')
    expect(pos.a).toEqual({ x: 999, y: 999 })
    expect(pos.b).toEqual(fresh.b)
    expect(pos.c).toEqual(fresh.c)
  })
})

describe('boundsOf', () => {
  it('pads the extent of the placed boxes', () => {
    expect(boundsOf(nodes, { a: { x: 0, y: 0 }, b: { x: 300, y: 100 } })).toEqual({ w: 500 + FRAME_PAD, h: 180 + FRAME_PAD })
  })
})
