// Pure half of the agent card (plan B.7): geometry, status marks and the flat prop bag that the
// live `AgentCard` message is projected onto. Kept out of the `ShapeUtil` module so `sync/apply.ts`
// can place cards without importing the renderer (and so the card can call back into `apply.ts`).
import type { AgentCard, AgentStatus } from '../state/types'
import { filesTouchedCount } from '../state/format'

/** B.7 geometry */
export const AGENT_CARD_W = 260
export const AGENT_CARD_H = 132
export const AGENT_CARD_MIN_W = 200
export const AGENT_CARD_MIN_H = 96

/** theme-invariant Geist 700 status marks (B.1 item 2) */
export const STATUS_COLORS: Record<AgentStatus, string> = {
  idle: 'var(--wb-idle)',
  working: 'var(--wb-working)',
  blocked: 'var(--wb-blocked)',
  done: 'var(--wb-done)',
}

export function statusColor(status: AgentStatus | string | null | undefined): string {
  return STATUS_COLORS[(status ?? 'idle') as AgentStatus] ?? STATUS_COLORS.idle
}

/**
 * class + spoken label for a card. `running` carries the conic ring (1.5s), `waiting` the same
 * ring at 4.5s — one mechanism, two meanings (B.1 item 3). Colour is never the only signal.
 */
export function agentCardState(status: AgentStatus | string | null | undefined): { cls: string; label: string } {
  switch (status) {
    case 'working':
      return { cls: 'running', label: 'working' }
    case 'blocked':
      return { cls: 'waiting', label: 'blocked' }
    case 'done':
      return { cls: 'done', label: 'done' }
    default:
      return { cls: 'idle', label: 'idle' }
  }
}

/** -1 means "no progress reported"; anything else is clamped to 0..1 */
export function clampProgress(progress: number | null | undefined): number {
  if (typeof progress !== 'number' || !Number.isFinite(progress)) return -1
  return Math.min(1, Math.max(0, progress))
}

/** every shape prop is a primitive (tldraw validates them one by one), so flatten the card here */
export interface AgentCardFacts {
  status: AgentStatus
  activity: string
  progress: number
  spawnedAt: string
  heartbeatAt: string
  finishedAt: string
  toolCalls: number
  filesTouched: number
  tokensIn: number
  tokensOut: number
  costUsd: number
}

export function agentCardFacts(agent: AgentCard): AgentCardFacts {
  const m = agent.metrics ?? {}
  return {
    status: agent.status,
    activity: agent.activity ?? '',
    progress: clampProgress(agent.progress),
    spawnedAt: agent.spawned_at ?? '',
    heartbeatAt: agent.heartbeat_at ?? '',
    finishedAt: agent.finished_at ?? '',
    toolCalls: numeric(m.tool_calls),
    filesTouched: filesTouchedCount(m),
    tokensIn: numeric(m.tokens_in),
    tokensOut: numeric(m.tokens_out),
    costUsd: numeric(m.cost_usd),
  }
}

function numeric(v: unknown): number {
  return typeof v === 'number' && Number.isFinite(v) ? v : 0
}
