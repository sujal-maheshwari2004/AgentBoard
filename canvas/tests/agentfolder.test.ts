// B.6's pure surface: the `g_` id round-trip, the collapse patch that reaches the sidecar, the
// two edit payloads, and how the folder picks the mermaid it shows. No editor, no rendering.
import { describe, expect, it } from 'vitest'
import { folderShapeId, parseShapeId, shapeMeta } from '../src/shapes/ids'
import {
  FOLDER_COLLAPSED_H,
  FOLDER_H,
  FOLDER_W,
  diagramEditPayload,
  extractFence,
  folderCollapsePatch,
  folderHue,
  folderMermaid,
  planEditPayload,
} from '../src/shapes/agentFolder'
import { agentCardFacts, agentCardState, clampProgress } from '../src/shapes/agentCard'
import type { AgentCard } from '../src/state/types'

describe('agent folder ids (B.6)', () => {
  it('uses its own `g_` prefix and round-trips', () => {
    expect(folderShapeId('agent-parser')).toBe('shape:g_agent-parser')
    expect(parseShapeId(folderShapeId('agent-parser'))).toEqual({ kind: 'agent-folder', id: 'agent-parser' })
  })

  it('does not collide with the frame (`f_`) or card (`a_`) namespaces', () => {
    expect(folderShapeId('agent-parser')).not.toBe('shape:f_agent-parser')
    expect(parseShapeId('shape:f_agent-parser')).toEqual({ kind: 'plan-frame', id: 'agent-parser' })
    expect(parseShapeId('shape:a_agent-parser')).toEqual({ kind: 'agent-card', id: 'agent-parser' })
  })

  it('stamps folder meta with the agent id as planId', () => {
    expect(shapeMeta('agent-folder', 'agent-parser', { ownerNode: 'node-parser' })).toEqual({
      kind: 'agent-folder',
      planId: 'agent-parser',
      ownerNode: 'node-parser',
    })
  })

  it('keeps B.6 geometry', () => {
    expect([FOLDER_W, FOLDER_H]).toEqual([320, 240])
    expect(FOLDER_COLLAPSED_H).toBe(36)
  })

  it('takes its hue from the agent status', () => {
    expect(folderHue('working')).toBe('var(--wb-working)')
    expect(folderHue('blocked')).toBe('var(--wb-blocked)')
    expect(folderHue('done')).toBe('var(--wb-done)')
    expect(folderHue(undefined)).toBe('var(--wb-idle)')
  })
})

describe('collapse patch (persisted as frames["<agent-id>"])', () => {
  const folder = { x: 120, y: 900, w: 320, h: 240 }

  it('records the height it is leaving when collapsing', () => {
    expect(folderCollapsePatch(folder, true)).toEqual({ x: 120, y: 900, w: 320, h: 240, collapsed: true })
  })

  it('restores the remembered height when expanding', () => {
    // the shape is 36px tall at this point; `expandedH` is what the sidecar must carry
    expect(folderCollapsePatch({ ...folder, h: FOLDER_COLLAPSED_H, expandedH: 300 }, false)).toEqual({
      x: 120,
      y: 900,
      w: 320,
      h: 300,
      collapsed: false,
    })
  })

  it('falls back to the current height when nothing was remembered', () => {
    expect(folderCollapsePatch({ ...folder, h: 180 }, false).h).toBe(180)
  })

  it('round-trips collapse → expand at the original height', () => {
    const shut = folderCollapsePatch(folder, true)
    const open = folderCollapsePatch({ x: shut.x, y: shut.y, w: shut.w, h: FOLDER_COLLAPSED_H, expandedH: shut.h }, false)
    expect(open).toEqual({ ...folder, collapsed: false })
  })
})

describe('edit payloads (A.6 client messages)', () => {
  it('plan edits carry the agent id and the whole document', () => {
    expect(planEditPayload('agent-parser', '# Job\n\nparse first')).toEqual({
      agent_id: 'agent-parser',
      plan_md: '# Job\n\nparse first',
    })
  })

  it('diagram edits carry mermaid only (the server rewrites the fence)', () => {
    expect(diagramEditPayload('agent-parser', 'flowchart TD\n  a --> b')).toEqual({
      agent_id: 'agent-parser',
      mermaid: 'flowchart TD\n  a --> b',
    })
  })
})

describe('the mermaid a folder shows', () => {
  it('prefers the server-parsed diagram', () => {
    expect(folderMermaid({ diagram: { mermaid: 'flowchart TD\n  x --> y' }, diagrams_md: '```\nstale\n```' })).toBe(
      'flowchart TD\n  x --> y',
    )
  })

  it('falls back to the first fence of diagrams.md', () => {
    expect(folderMermaid({ diagrams_md: '# Diagrams\n\n```mermaid\nflowchart TD\n  a --> b\n```\n' })).toBe(
      'flowchart TD\n  a --> b',
    )
  })

  it('is empty when there is no fence at all', () => {
    expect(folderMermaid({ diagrams_md: 'nothing here' })).toBe('')
    expect(extractFence('')).toBe('')
  })
})

describe('agent card facts (B.7)', () => {
  const agent: AgentCard = {
    id: 'agent-parser',
    assigned_node: 'node-parser',
    status: 'working',
    ready_deps: [],
    spawned_at: '2026-09-20T12:00:00.000Z',
    heartbeat_at: '2026-09-20T12:01:20.000Z',
    activity: 'writing the parser fence tests',
    progress: 0.62,
    metrics: { tool_calls: 14, files_touched: ['a', 'b'], tokens_in: 33_000, tokens_out: 8_200, cost_usd: 0.38 },
  }

  it('flattens the live card onto primitive shape props', () => {
    expect(agentCardFacts(agent)).toEqual({
      status: 'working',
      activity: 'writing the parser fence tests',
      progress: 0.62,
      spawnedAt: '2026-09-20T12:00:00.000Z',
      heartbeatAt: '2026-09-20T12:01:20.000Z',
      finishedAt: '',
      toolCalls: 14,
      filesTouched: 2,
      tokensIn: 33_000,
      tokensOut: 8_200,
      costUsd: 0.38,
    })
  })

  it('uses -1 for "no progress reported" and clamps the rest', () => {
    expect(clampProgress(undefined)).toBe(-1)
    expect(clampProgress(1.4)).toBe(1)
    expect(clampProgress(-2)).toBe(0)
    expect(agentCardFacts({ id: 'agent-x', assigned_node: null, status: 'idle', ready_deps: [] }).progress).toBe(-1)
  })

  it('maps status to the ring classes, each with a spoken label', () => {
    expect(agentCardState('working')).toEqual({ cls: 'running', label: 'working' })
    expect(agentCardState('blocked')).toEqual({ cls: 'waiting', label: 'blocked' })
    expect(agentCardState('done').cls).toBe('done')
    expect(agentCardState('idle').cls).toBe('idle')
    for (const s of ['idle', 'working', 'blocked', 'done'] as const) {
      expect(agentCardState(s).label.length).toBeGreaterThan(0)
    }
  })
})
