// Websocket client for CONTRACTS §6. Reconnects with full-jitter exponential backoff (cap 10 s),
// queues outbound messages while closed, sends `client.hello` on every open, tracks the last
// event seq it saw and drops stale event replays.
import type { ClientMessageType, ClientPayloads, Envelope, ServerMessage, SocketStatus } from '../state/types'

/** Server seqs below this are event seqs; at/above are per-connection counters (§6). */
export const EVENT_SEQ_LIMIT = 1_000_000

export interface WebSocketLike {
  readonly readyState: number
  onopen: ((ev: unknown) => void) | null
  onclose: ((ev: unknown) => void) | null
  onerror: ((ev: unknown) => void) | null
  onmessage: ((ev: { data: unknown }) => void) | null
  send(data: string): void
  close(code?: number, reason?: string): void
}
export type WebSocketCtor = new (url: string) => WebSocketLike

export interface SocketOptions {
  url: string
  onMessage: (msg: ServerMessage) => void
  onStatus?: (status: SocketStatus) => void
  clientId?: string
  /** injectable for tests; defaults to globalThis.WebSocket */
  WebSocket?: WebSocketCtor
  /** injectable timer for tests */
  setTimeout?: (fn: () => void, ms: number) => unknown
  clearTimeout?: (handle: unknown) => void
  random?: () => number
  baseDelayMs?: number
  maxDelayMs?: number
  now?: () => string
}

export interface Socket {
  send<T extends ClientMessageType>(type: T, payload: ClientPayloads[T]): number
  close(): void
  getLastSeq(): number
  getStatus(): SocketStatus
  readonly clientId: string
}

export function socketUrl(loc: { protocol: string; host: string } = window.location): string {
  return `${loc.protocol === 'https:' ? 'wss:' : 'ws:'}//${loc.host}/ws`
}

export function fullJitterDelay(attempt: number, base: number, cap: number, random: () => number): number {
  const exp = Math.min(cap, base * 2 ** attempt)
  return Math.floor(random() * exp)
}

function newClientId(): string {
  const c = globalThis.crypto
  if (c && typeof c.randomUUID === 'function') return c.randomUUID()
  return `c-${Math.random().toString(36).slice(2)}${Date.now().toString(36)}`
}

export function createSocket(opts: SocketOptions): Socket {
  const WS = opts.WebSocket ?? (globalThis.WebSocket as unknown as WebSocketCtor)
  const setT = opts.setTimeout ?? ((fn, ms) => setTimeout(fn, ms))
  const clearT = opts.clearTimeout ?? ((h) => clearTimeout(h as ReturnType<typeof setTimeout>))
  const random = opts.random ?? Math.random
  const base = opts.baseDelayMs ?? 250
  const cap = opts.maxDelayMs ?? 10_000
  const now = opts.now ?? (() => new Date().toISOString())
  const clientId = opts.clientId ?? newClientId()

  let ws: WebSocketLike | null = null
  let closed = false // closedRef-style guard: once closed, never reconnect or deliver
  let attempt = 0
  let seq = 0
  let lastSeq = 0
  let status: SocketStatus = 'connecting'
  let timer: unknown = null
  const queue: string[] = []

  const setStatus = (s: SocketStatus) => {
    if (status === s) return
    status = s
    opts.onStatus?.(s)
  }

  const isOpen = () => !!ws && ws.readyState === 1

  const envelope = (type: string, payload: unknown): Envelope => ({ type, payload, seq: ++seq, ts: now(), replyTo: null })

  const flush = () => {
    if (!isOpen() || !ws) return
    while (queue.length) {
      const next = queue.shift()!
      ws.send(next)
    }
  }

  const scheduleReconnect = () => {
    if (closed) return
    setStatus('reconnecting')
    const delay = fullJitterDelay(attempt, base, cap, random)
    attempt += 1
    timer = setT(() => {
      timer = null
      connect()
    }, delay)
  }

  const connect = () => {
    if (closed) return
    let sock: WebSocketLike
    try {
      sock = new WS(opts.url)
    } catch {
      scheduleReconnect()
      return
    }
    ws = sock
    sock.onopen = () => {
      if (closed || ws !== sock) {
        sock.close()
        return
      }
      attempt = 0
      setStatus('open')
      // hello goes first, ahead of anything queued while we were down
      sock.send(JSON.stringify(envelope('client.hello', { clientId, lastSeq, protocol: 1 })))
      flush()
    }
    sock.onmessage = (ev) => {
      if (closed || ws !== sock) return
      let msg: ServerMessage
      try {
        msg = JSON.parse(String(ev.data)) as ServerMessage
      } catch {
        return
      }
      if (!msg || typeof msg !== 'object' || typeof msg.type !== 'string') return
      if (typeof msg.seq === 'number' && msg.seq < EVENT_SEQ_LIMIT && msg.type === 'event.append') {
        if (msg.seq <= lastSeq) return // stale replay
        lastSeq = msg.seq
      }
      opts.onMessage(msg)
    }
    sock.onerror = () => {
      // close follows; nothing to do
    }
    sock.onclose = () => {
      if (ws === sock) ws = null
      if (closed) {
        setStatus('closed')
        return
      }
      scheduleReconnect()
    }
  }

  connect()

  return {
    clientId,
    send(type, payload) {
      const env = envelope(type, payload)
      const data = JSON.stringify(env)
      if (isOpen() && ws) ws.send(data)
      else queue.push(data)
      return env.seq
    },
    close() {
      if (closed) return
      closed = true
      if (timer != null) {
        clearT(timer)
        timer = null
      }
      const sock = ws
      ws = null
      queue.length = 0
      try {
        sock?.close()
      } catch {
        // ignore
      }
      setStatus('closed')
    },
    getLastSeq: () => lastSeq,
    getStatus: () => status,
  }
}
