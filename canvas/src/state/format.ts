// Narrow, locale-stable formatters for live monitoring (plan B.11 / B.7). Pure: no Date.now(),
// no store, no DOM — every elapsed readout takes the shared `nowMs` clock as an argument
// (B.1 pattern 7: one interval for the whole canvas, armed only while an agent is live).
//
// Conventions come from Langfuse (B.0): `∑` marks an aggregate, cost is Intl 2–6 dp, tokens
// read `in → out (∑ total)`.
import type { AgentCard, AgentMetrics } from './types'

/** A heartbeat older than this greys the activity line and appends `· stalled?` (B.7). */
export const STALE_MS = 90_000

const SECOND = 1000
const MINUTE = 60 * SECOND
const HOUR = 60 * MINUTE

function pad2(n: number): string {
  return n < 10 ? `0${n}` : String(n)
}

/**
 * `'42s'` under a minute, `'1m 23s'` under an hour, `'2h 04m'` above it. Never negative,
 * never `NaN` — an unparseable duration reads `'0s'`.
 */
export function formatElapsed(ms: number | null | undefined): string {
  if (typeof ms !== 'number' || !Number.isFinite(ms) || ms <= 0) return '0s'
  if (ms < MINUTE) return `${Math.floor(ms / SECOND)}s`
  if (ms < HOUR) {
    const m = Math.floor(ms / MINUTE)
    return `${m}m ${pad2(Math.floor((ms % MINUTE) / SECOND))}s`
  }
  const h = Math.floor(ms / HOUR)
  return `${h}h ${pad2(Math.floor((ms % HOUR) / MINUTE))}m`
}

const COST = new Intl.NumberFormat('en-US', {
  style: 'currency',
  currency: 'USD',
  minimumFractionDigits: 2,
  maximumFractionDigits: 6,
})

/** `$0.38`, `$0.001234` — Langfuse's cost rule (2–6 significant decimals). */
export function formatCost(usd: number | null | undefined): string {
  return COST.format(typeof usd === 'number' && Number.isFinite(usd) ? usd : 0)
}

const GROUPED = new Intl.NumberFormat('en-US')

export function formatCount(n: number | null | undefined): string {
  return GROUPED.format(typeof n === 'number' && Number.isFinite(n) ? Math.round(n) : 0)
}

/** `'1,234 → 567 (∑ 1,801)'` — the `∑` marks the aggregate (B.1 item 10). */
export function formatTokens(tokens: { in?: number | null; out?: number | null } | null | undefined): string {
  const i = num(tokens?.in)
  const o = num(tokens?.out)
  return `${formatCount(i)} → ${formatCount(o)} (∑ ${formatCount(i + o)})`
}

/** compact aggregate for the card's metrics row: `412`, `41.2k`, `1.4M` */
export function formatTokensCompact(total: number | null | undefined): string {
  const n = num(total)
  if (n < 1000) return String(Math.round(n))
  if (n < 1_000_000) return `${trim(n / 1000)}k`
  return `${trim(n / 1_000_000)}M`
}

function trim(n: number): string {
  return n.toFixed(1).replace(/\.0$/, '')
}

function num(v: unknown): number {
  return typeof v === 'number' && Number.isFinite(v) ? v : 0
}

/** ISO 8601 → epoch ms, or null when absent/unparseable */
export function parseTs(ts: string | null | undefined): number | null {
  if (!ts) return null
  const ms = Date.parse(ts)
  return Number.isFinite(ms) ? ms : null
}

/**
 * Wall clock for one agent, in ms: `spawned_at` → now while it runs (Phoenix: open work is
 * drawn to "now"), frozen at `finished_at` once it has finished. With no clock to read, the
 * last heartbeat stands in for now. `null` when the agent was never spawned.
 */
export function agentElapsed(
  card: Pick<AgentCard, 'spawned_at' | 'heartbeat_at' | 'finished_at'> | null | undefined,
  nowMs: number,
): number | null {
  const start = parseTs(card?.spawned_at)
  if (start === null) return null
  const finished = parseTs(card?.finished_at)
  if (finished !== null) return Math.max(0, finished - start)
  const now = Number.isFinite(nowMs) && nowMs > 0 ? nowMs : (parseTs(card?.heartbeat_at) ?? start)
  return Math.max(0, now - start)
}

/**
 * An agent whose clock must tick: running (or waiting) and not finished (Temporal's rule).
 * Lives here rather than in the store so `state/threads.ts` can ask the same question without
 * importing the store (`store.ts` re-exports both for its existing callers).
 */
export function isAgentLive(agent: AgentCard): boolean {
  if (agent.finished_at) return false
  return agent.status === 'working' || agent.status === 'blocked'
}

export function anyAgentLive(agents: Record<string, AgentCard>): boolean {
  return Object.values(agents).some(isAgentLive)
}

/** a live agent whose last heartbeat is older than 90s (B.7); finished agents never stall */
export function agentStalled(
  card: Pick<AgentCard, 'status' | 'heartbeat_at' | 'finished_at'> | null | undefined,
  nowMs: number,
  staleMs: number = STALE_MS,
): boolean {
  if (!card || card.finished_at || card.status === 'done' || card.status === 'idle') return false
  const beat = parseTs(card.heartbeat_at)
  if (beat === null) return false
  return nowMs - beat > staleMs
}

/** `files_touched` is a deduped list on the server (A.5) but may arrive as a plain count */
export function filesTouchedCount(metrics: AgentMetrics | null | undefined): number {
  const f = metrics?.files_touched
  if (Array.isArray(f)) return f.length
  return num(f)
}

/** `'14 tools · 6 files · ∑ 41.2k tok · $0.38'` — the card's metrics row (B.7) */
export function formatMetricsLine(metrics: AgentMetrics | null | undefined): string {
  const tokens = num(metrics?.tokens_in) + num(metrics?.tokens_out)
  return [
    `${formatCount(metrics?.tool_calls)} tools`,
    `${formatCount(filesTouchedCount(metrics))} files`,
    `∑ ${formatTokensCompact(tokens)} tok`,
    formatCost(metrics?.cost_usd),
  ].join(' · ')
}
