// Pure half of chat threading (plan B.9 / B.11). No React, no store, no DOM: the reducer case in
// `state/store.ts` and the `ChatPanel` both call into here, and `tests/threads.test.ts` pins it.
//
// The contract that shapes every function below: the server broadcasts one `chat.message` per
// `chat` event AND replays the last 50 per thread on `client.hello`, *before* the event replay.
// Backfill and live traffic therefore overlap on every reconnect, so a message is identified by
// its `id` and inserted in `seq` order. Threads are never built from `event.append`.
import type { AgentCard, ChatMessage, ClientPayloads } from './types'
import { isAgentLive } from './format'
import { statusColor } from '../shapes/agentCard'

/** the thread every non-agent conversation lands in */
export const ROOT_THREAD = 'root'

/** senders that speak for the root session rather than for a subagent */
const ROOT_SENDERS = new Set(['root', 'server'])

/** the thread a message belongs to; an empty/absent `thread` falls back to `root` */
export function threadOf(msg: Pick<ChatMessage, 'thread'>): string {
  const t = (msg.thread ?? '').trim()
  return t || ROOT_THREAD
}

/**
 * Merge one message into a thread, or return `null` when it is already there.
 *
 * `null` (rather than the unchanged array) is what lets the reducer return the *same* state
 * object for a duplicate, so React re-renders nothing on a reconnect's 50-message backfill.
 */
export function mergeChatMessage(list: ChatMessage[] | undefined, msg: ChatMessage): ChatMessage[] | null {
  const cur = list ?? []
  for (const m of cur) if (m.id === msg.id) return null
  // the common case is live traffic arriving in order; backfill after a gap is the slow path
  if (cur.length === 0 || cur[cur.length - 1].seq <= msg.seq) return [...cur, msg]
  let i = cur.length
  while (i > 0 && cur[i - 1].seq > msg.seq) i--
  return [...cur.slice(0, i), msg, ...cur.slice(i)]
}

/** unread bookkeeping: a cleared thread drops out of the map entirely */
export function clearUnread(unread: Record<string, number>, thread: string): Record<string, number> {
  if (!unread[thread]) return unread
  const next = { ...unread }
  delete next[thread]
  return next
}

export function bumpUnread(unread: Record<string, number>, thread: string): Record<string, number> {
  return { ...unread, [thread]: (unread[thread] ?? 0) + 1 }
}

export function totalUnread(unread: Record<string, number>): number {
  let n = 0
  for (const v of Object.values(unread)) n += v > 0 ? v : 0
  return n
}

export interface ThreadTab {
  thread: string
  label: string
  unread: number
  count: number
  /** the agent behind this tab is working or blocked (so it gets a tab even with no messages) */
  live: boolean
}

/**
 * The tab strip (B.9): `root` first, then every agent that has messages or is live, then any
 * other thread that has messages — each in a stable alphabetical order so tabs never jump.
 * The active thread always has a tab even when it is empty.
 */
export function deriveThreads(
  threads: Record<string, ChatMessage[]>,
  unread: Record<string, number>,
  agents: Record<string, AgentCard>,
  activeThread: string = ROOT_THREAD,
): ThreadTab[] {
  const names = new Set<string>([ROOT_THREAD])
  if (activeThread) names.add(activeThread)
  for (const [name, msgs] of Object.entries(threads)) if (msgs.length) names.add(name)
  for (const a of Object.values(agents)) if (isAgentLive(a)) names.add(a.id)

  const rest = [...names].filter((n) => n !== ROOT_THREAD).sort()
  return [ROOT_THREAD, ...rest].map((thread) => ({
    thread,
    label: thread === ROOT_THREAD ? 'root' : thread.replace(/^agent-/, ''),
    unread: unread[thread] ?? 0,
    count: threads[thread]?.length ?? 0,
    live: !!agents[thread] && isAgentLive(agents[thread]),
  }))
}

/** `chat.message {text, agentId, thread, reply_to}` — the composer's payload (B.9) */
export function chatSendPayload(
  text: string,
  thread: string,
  replyTo?: string | null,
): ClientPayloads['chat.message'] {
  const name = (thread ?? '').trim() || ROOT_THREAD
  const payload: ClientPayloads['chat.message'] = { text, thread: name }
  if (name !== ROOT_THREAD) payload.agentId = name
  if (replyTo) payload.reply_to = replyTo
  return payload
}

export interface ChatGroup {
  from: string
  /** the human's own messages, right-aligned */
  mine: boolean
  messages: ChatMessage[]
}

/** consecutive messages from one sender become one group with a single sender label (B.9) */
export function groupMessages(messages: ChatMessage[]): ChatGroup[] {
  const out: ChatGroup[] = []
  for (const m of messages) {
    const last = out[out.length - 1]
    if (last && last.from === m.from) last.messages.push(m)
    else out.push({ from: m.from, mine: m.from === 'user', messages: [m] })
  }
  return out
}

/** root speaks in the selection blue, an agent in its status colour (B.9) */
export function senderColor(from: string, agents: Record<string, AgentCard>): string {
  if (from === 'user') return 'var(--wb-text-2)'
  if (ROOT_SENDERS.has(from)) return 'var(--wb-selection)'
  const agent = agents[from]
  return agent ? statusColor(agent.status) : 'var(--wb-text-2)'
}

/** `HH:MM` in the viewer's own zone; `''` when the timestamp is unparseable */
export function formatClock(ts: string | null | undefined): string {
  if (!ts) return ''
  const ms = Date.parse(ts)
  if (!Number.isFinite(ms)) return ''
  const d = new Date(ms)
  return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
}

/** the one-line stub a `reply_to` renders above its bubble */
export function quotedStub(messages: ChatMessage[], replyTo: string | null | undefined, max = 60): string | null {
  if (!replyTo) return null
  const target = messages.find((m) => m.id === replyTo)
  if (!target) return `↩ ${replyTo}`
  const text = target.text.replace(/\s+/g, ' ').trim()
  return `↩ ${target.from}: ${text.length > max ? `${text.slice(0, max - 1)}…` : text}`
}
