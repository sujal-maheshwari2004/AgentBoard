import { describe, expect, it } from 'vitest'
import snapshotFixture from './fixtures/protocol/snapshot.json'
import { CLOCK_INTERVAL_MS, TICKER_LIMIT, anyAgentLive, createStore, initialState, isAgentLive, reduce, type StoreState, type StoreTimers } from '../src/state/store'
import { SERVER_MESSAGE_TYPES, type PlanSnapshot, type ServerMessage } from '../src/state/types'

const snap = snapshotFixture as unknown as PlanSnapshot

let counter = 1_000_000
function msg<T extends ServerMessage['type']>(type: T, payload: Extract<ServerMessage, { type: T }>['payload'], seq?: number): ServerMessage {
  return { type, payload, seq: seq ?? ++counter, ts: '2026-09-20T00:00:00.000Z', replyTo: null } as ServerMessage
}

const loaded = () => reduce(initialState(), msg('plan.snapshot', snap))

describe('reducer', () => {
  it('indexes a snapshot by id / edge key and picks the primary diagram', () => {
    const s = loaded()
    expect(s.hasSnapshot).toBe(true)
    expect(s.project).toBe(snap.project)
    expect(s.rev).toBe(7)
    expect(Object.keys(s.nodes)).toHaveLength(6)
    expect(s.edges['node-files__node-parser']).toMatchObject({ src: 'node-files', dst: 'node-parser' })
    expect(Object.keys(s.agents)).toEqual(['agent-files', 'agent-parser'])
    expect(s.activeBoard).toBe('hld')
    expect(s.layout.hld.frames?.['plan-board']).toMatchObject({ w: 1000, h: 700 })
    expect(s.ticker.at(-1)?.kind).toBe('snapshot')
  })

  it('handles node upsert/delete (and drops edges touching a deleted node)', () => {
    let s = loaded()
    s = reduce(s, msg('plan.node.upsert', { node: { ...snap.nodes[2], status: 'done' } }))
    expect(s.nodes['node-plan'].status).toBe('done')
    s = reduce(s, msg('plan.node.upsert', { node: { id: 'node-new', type: 'hld', title: 'New', status: 'todo', owner: null, depends_on: [], interfaces: [] } }))
    expect(s.nodes['node-new'].title).toBe('New')
    s = reduce(s, msg('plan.node.delete', { id: 'node-plan' }))
    expect(s.nodes['node-plan']).toBeUndefined()
    expect(Object.values(s.edges).some((e) => e.src === 'node-plan' || e.dst === 'node-plan')).toBe(false)
    expect(Object.keys(s.edges)).toHaveLength(3)
  })

  it('handles edge upsert/delete', () => {
    let s = loaded()
    s = reduce(s, msg('plan.edge.upsert', { edge: { src: 'node-canvas', dst: 'node-cli', label: 'ui', diagram: 'hld' } }))
    expect(s.edges['node-canvas__node-cli'].label).toBe('ui')
    s = reduce(s, msg('plan.edge.delete', { src: 'node-canvas', dst: 'node-cli', diagram: 'hld' }))
    expect(s.edges['node-canvas__node-cli']).toBeUndefined()
  })

  it('handles agent upsert/delete and clears a stale selection', () => {
    let s: StoreState = { ...loaded(), selectedAgentId: 'agent-parser' }
    s = reduce(s, msg('agent.card.upsert', { agent: { ...snap.agents[1], status: 'working' } }))
    expect(s.agents['agent-parser'].status).toBe('working')
    s = reduce(s, msg('agent.card.delete', { id: 'agent-parser' }))
    expect(s.agents['agent-parser']).toBeUndefined()
    expect(s.selectedAgentId).toBeNull()
  })

  it('appends events to the ticker (capped) and tracks lastSeq', () => {
    let s = loaded()
    for (let i = 1; i <= TICKER_LIMIT + 10; i++) {
      s = reduce(s, msg('event.append', { seq: i, ts: 't', agent_id: 'agent-x', node_id: null, type: 'info', note: `n${i}` }, i))
    }
    expect(s.ticker).toHaveLength(TICKER_LIMIT)
    expect(s.ticker.at(-1)?.text).toContain(`n${TICKER_LIMIT + 10}`)
    expect(s.lastSeq).toBe(TICKER_LIMIT + 10)
  })

  it('queues prompts, dispatches and risky edits without duplicates', () => {
    let s = loaded()
    const p = { prompt_id: 'p1', agent_id: 'agent-a', node_id: null, question: 'q?', kind: 'text' as const, choices: [] }
    s = reduce(s, msg('needs_input', p))
    s = reduce(s, msg('needs_input', p))
    expect(s.prompts).toHaveLength(1)
    const d = { request_id: 'd1', node_id: 'node-canvas', agent_id: 'agent-c', job_spec_md: '# job' }
    s = reduce(s, msg('dispatch.request', d))
    s = reduce(s, msg('dispatch.request', d))
    expect(s.dispatches).toHaveLength(1)
    const r = { request_id: 'r1', summary: 'x depends_on -y', diff: '-y', affected: ['node-x'] }
    s = reduce(s, msg('risky_edit.request', r))
    s = reduce(s, msg('risky_edit.request', r))
    expect(s.riskyEdits).toHaveLength(1)
    expect(s.ticker.filter((t) => ['needs_input', 'dispatch', 'risky_edit'].includes(t.kind))).toHaveLength(3)
  })

  it('handles ack / reject / layout / error / bridge', () => {
    let s = loaded()
    s = reduce(s, msg('edit.ack', { forSeq: 3, rev: 8 }))
    expect(s.rev).toBe(8)
    expect(s.lastAck).toEqual({ forSeq: 3, rev: 8 })
    s = reduce(s, msg('edit.reject', { forSeq: 4, reason: 'nope', revert: [] }))
    expect(s.lastReject).toMatchObject({ forSeq: 4, reason: 'nope' })
    s = reduce(s, msg('layout.update', { diagram: 'hld', layout: { version: 1, nodes: { 'node-a': { x: 1, y: 2 } } } }))
    expect(s.layout.hld.nodes).toEqual({ 'node-a': { x: 1, y: 2 } })
    s = reduce(s, msg('server.error', { code: 'bad', message: 'boom' }))
    expect(s.lastError).toMatchObject({ code: 'bad', message: 'boom' })
    s = reduce(s, msg('bridge.status', { ok: false, failures: 2, last_error: 'socket gone' }))
    expect(s.bridge).toEqual({ ok: false, failures: 2, last_error: 'socket gone' })
  })

  it('has a case for every server message type in CONTRACTS §6 (none falls through unchanged)', () => {
    const base = loaded()
    const payloads: Record<ServerMessage['type'], ServerMessage['payload']> = {
      'plan.snapshot': { ...snap, rev: 99 },
      'plan.node.upsert': { node: { ...snap.nodes[0], title: 'changed' } },
      'plan.node.delete': { id: 'node-cli' },
      'plan.edge.upsert': { edge: { src: 'node-cli', dst: 'node-files', diagram: 'hld' } },
      'plan.edge.delete': { src: 'node-files', dst: 'node-parser', diagram: 'hld' },
      'agent.card.upsert': { agent: { ...snap.agents[0], status: 'blocked' } },
      'agent.card.delete': { id: 'agent-files' },
      'event.append': { seq: 1, ts: 't', agent_id: 'root', node_id: null, type: 'info', note: '' },
      needs_input: { prompt_id: 'p', agent_id: 'a', node_id: null, question: 'q', kind: 'confirm', choices: [] },
      'dispatch.request': { request_id: 'd', node_id: 'n', agent_id: 'a', job_spec_md: '' },
      'risky_edit.request': { request_id: 'r', summary: 's', diff: '', affected: [] },
      'edit.ack': { forSeq: 1, rev: 100 },
      'edit.reject': { forSeq: 1, reason: 'r', revert: [] },
      'layout.update': { diagram: 'hld', layout: { version: 2 } },
      'server.error': { code: 'c', message: 'm' },
      'bridge.status': { ok: true, failures: 0 },
    }
    for (const type of SERVER_MESSAGE_TYPES) {
      const next = reduce(base, { type, payload: payloads[type], seq: 1, ts: 't', replyTo: null } as ServerMessage)
      expect(next, type).not.toBe(base)
    }
    const unknown = reduce(base, { type: 'nope', payload: {}, seq: 1, ts: 't' } as unknown as ServerMessage)
    expect(unknown).toBe(base)
  })
})

describe('createStore', () => {
  it('notifies subscribers on dispatch and on UI setters', () => {
    const s = createStore()
    let n = 0
    const off = s.subscribe(() => n++)
    s.dispatch(msg('plan.snapshot', snap))
    expect(n).toBe(1)
    s.selectAgent('agent-parser')
    expect(s.getState().selectedAgentId).toBe('agent-parser')
    s.setSocketStatus('open')
    s.setSocketStatus('open') // unchanged → no emit
    expect(n).toBe(3)
    s.dispatch(msg('needs_input', { prompt_id: 'p1', agent_id: 'a', node_id: null, question: 'q', kind: 'text', choices: [] }))
    s.removePrompt('p1')
    expect(s.getState().prompts).toEqual([])
    s.dispatch(msg('dispatch.request', { request_id: 'd1', node_id: 'n', agent_id: 'a', job_spec_md: '' }))
    s.removeDispatch('d1')
    s.dispatch(msg('risky_edit.request', { request_id: 'r1', summary: '', diff: '', affected: [] }))
    s.removeRiskyEdit('r1')
    expect(s.getState().dispatches).toEqual([])
    expect(s.getState().riskyEdits).toEqual([])
    s.note('chat', 'hello')
    expect(s.getState().ticker.at(-1)).toMatchObject({ kind: 'chat', text: 'hello' })
    off()
    const before = n
    s.selectAgent(null)
    expect(n).toBe(before)
  })
})

// ---- B.11 pattern 7: ONE 1s clock, armed only while an agent is live ----

function fakeTimers(): StoreTimers & { fire(): void; armed: number; cleared: number } {
  let tick: (() => void) | null = null
  let t = 1_000_000
  const timers = {
    armed: 0,
    cleared: 0,
    setInterval(fn: () => void, ms: number) {
      expect(ms).toBe(CLOCK_INTERVAL_MS)
      timers.armed++
      tick = fn
      return 'h'
    },
    clearInterval() {
      timers.cleared++
      tick = null
    },
    now: () => t,
    fire() {
      t += CLOCK_INTERVAL_MS
      tick?.()
    },
  }
  return timers
}

describe('the shared nowMs clock', () => {
  it('knows which agents are live', () => {
    expect(isAgentLive({ id: 'a', assigned_node: null, status: 'working', ready_deps: [] })).toBe(true)
    expect(isAgentLive({ id: 'a', assigned_node: null, status: 'blocked', ready_deps: [] })).toBe(true)
    expect(isAgentLive({ id: 'a', assigned_node: null, status: 'idle', ready_deps: [] })).toBe(false)
    expect(isAgentLive({ id: 'a', assigned_node: null, status: 'done', ready_deps: [] })).toBe(false)
    // a finished agent is never live, whatever its status still says
    expect(isAgentLive({ id: 'a', assigned_node: null, status: 'working', ready_deps: [], finished_at: 'x' })).toBe(false)
    expect(anyAgentLive({})).toBe(false)
  })

  it('arms one interval on the first live agent and clears it when the last one finishes', () => {
    const timers = fakeTimers()
    const s = createStore(initialState(), timers)
    // the fixture ships one working agent (agent-parser), so the snapshot alone arms the clock
    s.dispatch(msg('plan.snapshot', snap))
    expect(timers.armed).toBe(1)
    const t0 = s.getState().nowMs
    timers.fire()
    expect(s.getState().nowMs).toBe(t0 + CLOCK_INTERVAL_MS)

    // a second live agent does not arm a second interval
    s.dispatch(msg('agent.card.upsert', { agent: { ...snap.agents[0], status: 'working' as const, finished_at: null } }))
    expect(timers.armed).toBe(1)

    s.dispatch(msg('agent.card.upsert', { agent: { ...snap.agents[1], status: 'done' as const } }))
    expect(timers.cleared).toBe(0) // agent-files is still working
    s.dispatch(msg('agent.card.delete', { id: 'agent-files' }))
    expect(timers.cleared).toBe(1)

    // and it re-arms for the next live agent
    s.dispatch(msg('agent.card.upsert', { agent: { ...snap.agents[1], status: 'working' as const } }))
    expect(timers.armed).toBe(2)
    s.stopClock()
    expect(timers.cleared).toBe(2)
  })
})
