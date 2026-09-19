import { describe, expect, it } from 'vitest'
import { EVENT_SEQ_LIMIT, createSocket, fullJitterDelay, socketUrl, type WebSocketLike } from '../src/ws/client'
import type { Envelope, ServerMessage } from '../src/state/types'

// ---- fake WebSocket + manual timers ----

class FakeWS implements WebSocketLike {
  static instances: FakeWS[] = []
  readyState = 0
  sent: string[] = []
  closed = false
  onopen: ((ev: unknown) => void) | null = null
  onclose: ((ev: unknown) => void) | null = null
  onerror: ((ev: unknown) => void) | null = null
  onmessage: ((ev: { data: unknown }) => void) | null = null
  constructor(public url: string) {
    FakeWS.instances.push(this)
  }
  send(data: string) {
    if (this.readyState !== 1) throw new Error('not open')
    this.sent.push(data)
  }
  close() {
    this.closed = true
    this.readyState = 3
    this.onclose?.({})
  }
  // test helpers
  open() {
    this.readyState = 1
    this.onopen?.({})
  }
  drop() {
    this.readyState = 3
    this.onclose?.({})
  }
  receive(msg: Partial<ServerMessage> & { type: string; seq: number }) {
    this.onmessage?.({ data: JSON.stringify({ ts: 't', payload: {}, ...msg }) })
  }
  envelopes(): Envelope[] {
    return this.sent.map((s) => JSON.parse(s) as Envelope)
  }
}

function timers() {
  const queue: Array<{ fn: () => void; ms: number; id: number }> = []
  let id = 0
  return {
    queue,
    setTimeout: (fn: () => void, ms: number) => {
      queue.push({ fn, ms, id: ++id })
      return id
    },
    clearTimeout: (h: unknown) => {
      const i = queue.findIndex((q) => q.id === h)
      if (i >= 0) queue.splice(i, 1)
    },
    runNext() {
      const next = queue.shift()
      next?.fn()
      return next?.ms
    },
  }
}

function make(extra: Partial<Parameters<typeof createSocket>[0]> = {}) {
  FakeWS.instances = []
  const t = timers()
  const received: ServerMessage[] = []
  const statuses: string[] = []
  const socket = createSocket({
    url: 'ws://test/ws',
    clientId: 'client-1',
    WebSocket: FakeWS,
    setTimeout: t.setTimeout,
    clearTimeout: t.clearTimeout,
    random: () => 1,
    onMessage: (m) => received.push(m),
    onStatus: (s) => statuses.push(s),
    ...extra,
  })
  return { socket, t, received, statuses, ws: () => FakeWS.instances.at(-1)! }
}

describe('createSocket', () => {
  it('builds the url from location', () => {
    expect(socketUrl({ protocol: 'http:', host: 'localhost:5173' })).toBe('ws://localhost:5173/ws')
    expect(socketUrl({ protocol: 'https:', host: 'example.com' })).toBe('wss://example.com/ws')
  })

  it('queues sends while connecting and flushes them after hello on open', () => {
    const { socket, ws, statuses } = make()
    const s1 = socket.send('chat.message', { text: 'hi' })
    const s2 = socket.send('plan.paste', { text: 'flowchart TD' })
    expect([s1, s2]).toEqual([1, 2])
    expect(ws().sent).toEqual([])
    ws().open()
    const env = ws().envelopes()
    expect(env.map((e) => e.type)).toEqual(['client.hello', 'chat.message', 'plan.paste'])
    expect(env[0].payload).toEqual({ clientId: 'client-1', lastSeq: 0, protocol: 1 })
    expect(env[0].seq).toBe(3) // hello takes the next seq at open time
    expect(env[1].seq).toBe(1)
    expect(env[2].seq).toBe(2)
    expect(statuses).toEqual(['open'])
  })

  it('sends directly when open and returns an increasing seq', () => {
    const { socket, ws } = make()
    ws().open()
    const a = socket.send('node.status', { id: 'node-a', status: 'done' })
    const b = socket.send('plan.relayout', { diagram: 'hld' })
    expect(b).toBe(a + 1)
    expect(ws().envelopes().at(-1)).toMatchObject({ type: 'plan.relayout', seq: b, payload: { diagram: 'hld' } })
  })

  it('reconnects with capped backoff and re-sends hello with the last event seq', () => {
    const { socket, ws, t, received, statuses } = make({ baseDelayMs: 250, maxDelayMs: 10_000 })
    const first = ws()
    first.open()
    first.receive({ type: 'event.append', seq: 41, payload: { seq: 41 } as never })
    first.receive({ type: 'event.append', seq: 42, payload: { seq: 42 } as never })
    expect(socket.getLastSeq()).toBe(42)
    first.drop()
    expect(statuses).toEqual(['open', 'reconnecting'])
    expect(t.queue).toHaveLength(1)
    // random() = 1 → full delay: 250 * 2^attempt, capped at 10 s
    expect(t.runNext()).toBe(250)
    const second = ws()
    expect(second).not.toBe(first)
    second.drop()
    expect(t.runNext()).toBe(500)
    ws().drop()
    expect(t.runNext()).toBe(1000)
    for (let i = 0; i < 6; i++) {
      ws().drop()
      t.runNext()
    }
    ws().drop()
    expect(t.runNext()).toBe(10_000)
    const last = ws()
    socket.send('chat.message', { text: 'queued while down' })
    last.open()
    const env = last.envelopes()
    expect(env[0]).toMatchObject({ type: 'client.hello', payload: { clientId: 'client-1', lastSeq: 42, protocol: 1 } })
    expect(env[1]).toMatchObject({ type: 'chat.message' })
    expect(received).toHaveLength(2)
    expect(socket.getStatus()).toBe('open')
  })

  it('drops stale event replays but passes non-event messages through', () => {
    const { ws, received } = make()
    ws().open()
    ws().receive({ type: 'event.append', seq: 10, payload: { seq: 10 } as never })
    ws().receive({ type: 'event.append', seq: 10, payload: { seq: 10 } as never })
    ws().receive({ type: 'event.append', seq: 9, payload: { seq: 9 } as never })
    ws().receive({ type: 'plan.node.upsert', seq: 10, payload: { node: {} } as never }) // same seq, not an event → kept
    ws().receive({ type: 'edit.ack', seq: EVENT_SEQ_LIMIT + 1, payload: { forSeq: 1, rev: 2 } })
    ws().receive({ type: 'edit.ack', seq: EVENT_SEQ_LIMIT + 1, payload: { forSeq: 1, rev: 2 } })
    expect(received.map((m) => `${m.type}:${m.seq}`)).toEqual(['event.append:10', 'plan.node.upsert:10', 'edit.ack:1000001', 'edit.ack:1000001'])
    ws().onmessage?.({ data: 'not json' })
    ws().onmessage?.({ data: '{"noType":true}' })
    expect(received).toHaveLength(4)
  })

  it('close() is final: no reconnect, late open is shut, nothing delivered (StrictMode guard)', () => {
    const { socket, ws, t, received, statuses } = make()
    const sock = ws()
    socket.close()
    expect(sock.closed).toBe(true)
    expect(t.queue).toHaveLength(0)
    expect(statuses.at(-1)).toBe('closed')
    // a socket that reports open after close is closed again and ignored
    sock.closed = false
    sock.readyState = 1
    sock.onopen?.({})
    expect(sock.closed).toBe(true)
    sock.onmessage?.({ data: JSON.stringify({ type: 'edit.ack', seq: 1, ts: 't', payload: {} }) })
    expect(received).toEqual([])
    socket.close() // idempotent
    expect(FakeWS.instances).toHaveLength(1)
  })

  it('cancels a pending reconnect timer on close', () => {
    const { socket, ws, t } = make()
    ws().open()
    ws().drop()
    expect(t.queue).toHaveLength(1)
    socket.close()
    expect(t.queue).toHaveLength(0)
  })
})

describe('fullJitterDelay', () => {
  it('is bounded by min(cap, base * 2^attempt)', () => {
    expect(fullJitterDelay(0, 250, 10_000, () => 1)).toBe(250)
    expect(fullJitterDelay(3, 250, 10_000, () => 1)).toBe(2000)
    expect(fullJitterDelay(20, 250, 10_000, () => 1)).toBe(10_000)
    expect(fullJitterDelay(5, 250, 10_000, () => 0)).toBe(0)
    const r = fullJitterDelay(4, 250, 10_000, () => 0.5)
    expect(r).toBe(2000)
  })
})
