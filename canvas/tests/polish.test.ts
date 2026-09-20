// B.S6 — the pure half of the polish pass: board content bounds and the load camera, the feed's
// collapsed-by-default state, island geometry, a grouped diff, reduced motion and the spoken
// description of an edge.
import { describe, expect, it } from 'vitest'
import {
  BOARD_CONTENT_PAD,
  BOARD_MIN_H,
  BOARD_MIN_W,
  boardForViewport,
  contentSize,
  contentViewBox,
  type BoxLike,
} from '../src/sync/boards'
import { cameraAnimation, prefersReducedMotion } from '../src/sync/motion'
import { FEED_DEFAULT_OPEN, feedSummary } from '../src/panels/feed'
import { CHAT_ISLAND_H, CHAT_PILL_H, chatHeightValue, chatIslandHeight, defaultOpenSections, toggleSection } from '../src/panels/islands'
import {
  CHANGES_GROUP_THRESHOLD,
  diagramChangeLines,
  groupChangeLines,
  shouldGroupChanges,
} from '../src/panels/diagramChanges'
import { edgeDescription } from '../src/shapes/ShapeWrapper'
import type { DiagramRequest, PlanEvent } from '../src/state/types'

// ---- board content bounds (item 2) ----

describe('contentSize', () => {
  it('falls back to the minimum for an empty board', () => {
    expect(contentSize([])).toEqual({ w: BOARD_MIN_W, h: BOARD_MIN_H })
  })

  it('never goes below the minimum, however small the content', () => {
    expect(contentSize([{ x: 0, y: 0, w: 200, h: 64 }])).toEqual({ w: BOARD_MIN_W, h: BOARD_MIN_H })
  })

  it('grows to the content extent plus one pad', () => {
    const size = contentSize([{ x: 900, y: 700, w: 200, h: 64 }], { pad: 40 })
    expect(size).toEqual({ w: 1140, h: 804 })
  })

  it('reserves the strip the node label hangs in', () => {
    const plain = contentSize([{ x: 900, y: 700, w: 200, h: 64 }], { pad: 40 })
    const withLabel = contentSize([{ x: 900, y: 700, w: 200, h: 64 }], { pad: 40, extra: 40 })
    expect(withLabel.h - plain.h).toBe(40)
  })

  it('takes the maximum over every child, not the last one', () => {
    const size = contentSize(
      [
        { x: 0, y: 0, w: 200, h: 64 },
        { x: 1400, y: 900, w: 200, h: 64 },
        { x: 40, y: 40, w: 200, h: 64 },
      ],
      { pad: BOARD_CONTENT_PAD },
    )
    expect(size.w).toBe(1600 + BOARD_CONTENT_PAD)
    expect(size.h).toBe(964 + BOARD_CONTENT_PAD)
  })
})

describe('contentViewBox (the camera on load)', () => {
  const frame: BoxLike = { x: 0, y: 0, w: 1600, h: 1000 }

  it('frames the whole board when it has no content', () => {
    expect(contentViewBox(frame, [])).toEqual(frame)
  })

  it('frames the content, not the frame — this is what made boards read as empty', () => {
    const box = contentViewBox(frame, [{ x: 400, y: 300, w: 200, h: 64 }], { pad: 40 })
    expect(box).toEqual({ x: 360, y: 260, w: 280, h: 144 })
  })

  it('converts frame-local children into page coordinates', () => {
    const lld: BoxLike = { x: 1800, y: 0, w: 1600, h: 1000 }
    const box = contentViewBox(lld, [{ x: 100, y: 100, w: 200, h: 64 }], { pad: 20 })
    expect(box.x).toBe(1800 + 80)
    expect(box.y).toBe(80)
  })

  it('never frames outside the board and never returns a zero-sized box', () => {
    const box = contentViewBox(frame, [{ x: 0, y: 0, w: 1600, h: 1000 }], { pad: 200 })
    expect(box.x).toBe(0)
    expect(box.y).toBe(0)
    expect(box.w).toBeLessThanOrEqual(frame.w)
    expect(box.h).toBeLessThanOrEqual(frame.h)
  })
})

describe('boardForViewport (the switcher when zoomed far out)', () => {
  const boards = [
    { name: 'hld' as const, box: { x: 0, y: 0, w: 1000, h: 800 } },
    { name: 'lld' as const, box: { x: 1800, y: 0, w: 1000, h: 800 } },
  ]

  it('takes the board under the viewport centre', () => {
    expect(boardForViewport(boards, { x: 0, y: 0, w: 800, h: 600 })).toBe('hld')
    expect(boardForViewport(boards, { x: 1800, y: 0, w: 800, h: 600 })).toBe('lld')
  })

  it('falls back to the most visible board when the centre is in the gutter', () => {
    // centred at x=1400 (between the two), but far more of HLD is on screen
    expect(boardForViewport(boards, { x: 0, y: 0, w: 2800, h: 1600 })).toBe('hld')
    expect(boardForViewport(boards, { x: 1200, y: 0, w: 1600, h: 1600 })).toBe('lld')
  })

  it('is null only when no board is on screen at all', () => {
    expect(boardForViewport(boards, { x: 9000, y: 9000, w: 100, h: 100 })).toBeNull()
    expect(boardForViewport([], { x: 0, y: 0, w: 100, h: 100 })).toBeNull()
  })
})

// ---- the event feed (item 1) ----

function ev(seq: number, type: string, note: string, agent = 'agent-parser'): PlanEvent {
  return { seq, ts: `2026-09-20T00:00:${String(seq).padStart(2, '0')}Z`, agent_id: agent, node_id: null, type, note }
}

describe('event feed', () => {
  it('is collapsed on load so it cannot cover the first board', () => {
    expect(FEED_DEFAULT_OPEN).toBe(false)
  })

  it('says how many events there are and what the last one was', () => {
    const s = feedSummary([ev(1, 'spawned', 'agent-parser started'), ev(2, 'heartbeat', 'writing tests')])
    expect(s.count).toBe(2)
    expect(s.text).toBe('agent-parser heartbeat: writing tests')
    expect(s.tone).toBe('plain')
    expect(s.label).toContain('Events (2)')
  })

  it('lets a server error win the pill and tints it', () => {
    const s = feedSummary([ev(1, 'heartbeat', 'working')], [], { code: 'bad_diagram', message: 'cycle' })
    expect(s.tone).toBe('err')
    expect(s.text).toBe('bad_diagram: cycle')
  })

  it('falls back to the local ticker, then to "waiting"', () => {
    expect(feedSummary([], [{ seq: -1, ts: 'now', kind: 'edit', text: 'sent renamed' }]).text).toBe('edit sent renamed')
    expect(feedSummary([]).text).toBe('waiting for the server…')
    expect(feedSummary([]).count).toBe(0)
  })

  it('clips a long line rather than stretching the pill', () => {
    const s = feedSummary([ev(1, 'info', 'x'.repeat(200))])
    expect(s.text.length).toBeLessThanOrEqual(48)
    expect(s.text.endsWith('…')).toBe(true)
  })
})

// ---- island geometry (items 3 and 4) ----

describe('island geometry', () => {
  it('drives the chat island height from one number', () => {
    expect(chatIslandHeight(true)).toBe(CHAT_ISLAND_H)
    expect(chatIslandHeight(false)).toBe(CHAT_PILL_H)
    expect(chatHeightValue(true)).toBe('420px')
    expect(chatHeightValue(false)).toBe('44px')
  })

  it('opens the inspector on the feed, with both editors one click away', () => {
    const open = defaultOpenSections()
    expect(open).toEqual({ feed: true, plan: false, diagram: false })
    expect(toggleSection(open, 'plan')).toEqual({ feed: true, plan: true, diagram: false })
    expect(toggleSection(toggleSection(open, 'feed'), 'feed')).toEqual(open)
  })
})

// ---- a large diff (item 10) ----

function request(nodes: number, edges: number): DiagramRequest {
  return {
    request_id: 'r1',
    name: 'lld',
    mermaid: 'flowchart TD',
    rationale: '',
    nodes_added: Array.from({ length: nodes }, (_, i) => ({ id: `node-${i}`, label: `N${i}` })),
    nodes_removed: ['node-legacy'],
    edges_added: Array.from({ length: edges }, (_, i) => ({ src: `node-${i}`, dst: 'node-sink' })),
    edges_removed: [],
  }
}

describe('grouping a large diff', () => {
  it('leaves a small diff flat', () => {
    expect(shouldGroupChanges(diagramChangeLines(request(2, 2)))).toBe(false)
  })

  it('groups once the list is longer than the threshold', () => {
    const lines = diagramChangeLines(request(8, 6))
    expect(lines.length).toBeGreaterThan(CHANGES_GROUP_THRESHOLD)
    expect(shouldGroupChanges(lines)).toBe(true)
    const groups = groupChangeLines(lines)
    expect(groups.map((g) => g.key)).toEqual(['added-node', 'removed-node', 'added-edge'])
    expect(groups[0].title).toBe('8 nodes added')
    expect(groups[1].title).toBe('1 node removed')
    expect(groups.reduce((n, g) => n + g.lines.length, 0)).toBe(lines.length)
  })

  it('starts a big group collapsed and a small one open', () => {
    const groups = groupChangeLines(diagramChangeLines(request(8, 2)))
    expect(groups[0].defaultOpen).toBe(false)
    expect(groups[1].defaultOpen).toBe(true)
  })

  it('drops empty buckets', () => {
    expect(groupChangeLines([]).length).toBe(0)
  })
})

// ---- reduced motion (item 13) ----

describe('reduced motion', () => {
  const reduce = { matchMedia: (q: string) => ({ matches: q.includes('reduced-motion') }) }
  const normal = { matchMedia: () => ({ matches: false }) }

  it('reads the media query', () => {
    expect(prefersReducedMotion(reduce)).toBe(true)
    expect(prefersReducedMotion(normal)).toBe(false)
  })

  it('tolerates an environment without matchMedia', () => {
    expect(prefersReducedMotion({})).toBe(false)
    expect(
      prefersReducedMotion({
        matchMedia: () => {
          throw new Error('nope')
        },
      }),
    ).toBe(false)
  })

  it('zeroes every camera flight when motion is reduced', () => {
    expect(cameraAnimation(300, normal)).toEqual({ duration: 300 })
    expect(cameraAnimation(300, reduce)).toEqual({ duration: 0 })
  })
})

// ---- edges carry words, not just colour (item 12) ----

describe('edgeDescription', () => {
  it('spells out the state an edge encodes in colour', () => {
    expect(edgeDescription('node-a__node-b', 'done', false)).toBe('dependency node-a → node-b — dependency satisfied')
    expect(edgeDescription('node-a__node-b', 'working', false)).toBe('dependency node-a → node-b — source running')
    expect(edgeDescription('node-a__node-b', 'blocked', true)).toBe('dependency node-a → node-b — target blocked, cross-board')
    expect(edgeDescription('', 'default', false)).toBe('dependency dependency — not started')
  })
})
