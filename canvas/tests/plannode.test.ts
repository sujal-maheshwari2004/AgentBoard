// B.3's per-status table and B.4's edge rules, as pure functions (no editor, no rendering).
import { describe, expect, it } from 'vitest'
import { planNodeState } from '../src/shapes/PlanNodeUtil'
import { edgeState } from '../src/sync/apply'
import { DEGRADE_ZOOM, borderAlpha } from '../src/sync/zoom'

describe('planNodeState (B.3)', () => {
  it('draws an undispatched todo node as the dotted "not started" state', () => {
    expect(planNodeState({ status: 'todo', ready: false, dispatched: false })).toEqual({
      cls: 'undispatched',
      label: 'not dispatched',
    })
  })

  it('prefers ready over both todo states', () => {
    expect(planNodeState({ status: 'todo', ready: true, dispatched: false }).cls).toBe('ready')
    expect(planNodeState({ status: 'todo', ready: true, dispatched: true }).cls).toBe('ready')
  })

  it('a dispatched todo node is idle, not dotted', () => {
    expect(planNodeState({ status: 'todo', ready: false, dispatched: true }).cls).toBe('idle')
  })

  it('maps the live statuses to the ring / done classes', () => {
    expect(planNodeState({ status: 'in_progress', ready: false, dispatched: true }).cls).toBe('running')
    expect(planNodeState({ status: 'blocked', ready: false, dispatched: true }).cls).toBe('waiting')
    expect(planNodeState({ status: 'done', ready: false, dispatched: true }).cls).toBe('done')
  })

  it('every state carries a spoken label, so colour is never the only signal', () => {
    for (const status of ['todo', 'in_progress', 'blocked', 'done'] as const) {
      for (const ready of [true, false]) {
        const s = planNodeState({ status, ready, dispatched: false })
        expect(s.label.length).toBeGreaterThan(0)
      }
    }
  })
})

describe('edgeState (B.4)', () => {
  it('is neutral while nothing has happened', () => {
    expect(edgeState('todo', 'todo')).toBe('default')
  })
  it('marches while the source is running', () => {
    expect(edgeState('in_progress', 'todo')).toBe('working')
  })
  it('goes green once the dependency is satisfied', () => {
    expect(edgeState('done', 'todo')).toBe('done')
  })
  it('lights up the failing path: a blocked destination wins over the source', () => {
    expect(edgeState('done', 'blocked')).toBe('blocked')
    expect(edgeState('in_progress', 'blocked')).toBe('blocked')
  })
})

describe('borderAlpha (B.3 zoom ramp)', () => {
  it('is 0.1 at zoom 1 and above', () => {
    expect(borderAlpha(1)).toBeCloseTo(0.1, 5)
    expect(borderAlpha(2)).toBeCloseTo(0.1, 5)
  })
  it('is 0.7 at zoom 0.2 and below', () => {
    expect(borderAlpha(0.2)).toBeCloseTo(0.7, 5)
    expect(borderAlpha(0.05)).toBeCloseTo(0.7, 5)
  })
  it('rises monotonically as you zoom out', () => {
    const samples = [1, 0.8, 0.6, 0.4, 0.3, 0.2].map(borderAlpha)
    for (let i = 1; i < samples.length; i++) expect(samples[i]).toBeGreaterThan(samples[i - 1])
  })
  it('degrades to a dot below quarter zoom', () => {
    expect(DEGRADE_ZOOM).toBe(0.25)
  })
})
