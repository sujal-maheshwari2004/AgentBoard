// B.11's formatters and the shared-clock helpers they feed (pure: no Date.now(), no store).
import { describe, expect, it } from 'vitest'
import {
  STALE_MS,
  agentElapsed,
  agentStalled,
  filesTouchedCount,
  formatCost,
  formatCount,
  formatElapsed,
  formatMetricsLine,
  formatTokens,
  formatTokensCompact,
} from '../src/state/format'
import type { AgentCard } from '../src/state/types'

const T0 = Date.parse('2026-09-20T12:00:00.000Z')

function card(patch: Partial<AgentCard> = {}): AgentCard {
  return {
    id: 'agent-parser',
    assigned_node: 'node-parser',
    status: 'working',
    ready_deps: [],
    spawned_at: '2026-09-20T12:00:00.000Z',
    ...patch,
  }
}

describe('formatElapsed', () => {
  it('reads seconds under a minute', () => {
    expect(formatElapsed(42_000)).toBe('42s')
    expect(formatElapsed(999)).toBe('0s')
    expect(formatElapsed(59_999)).toBe('59s')
  })

  it('reads minutes and zero-padded seconds under an hour', () => {
    expect(formatElapsed(83_000)).toBe('1m 23s')
    expect(formatElapsed(63_000)).toBe('1m 03s')
    expect(formatElapsed(60 * 60_000 - 1)).toBe('59m 59s')
  })

  it('reads hours and zero-padded minutes above an hour', () => {
    expect(formatElapsed(2 * 3_600_000 + 4 * 60_000)).toBe('2h 04m')
    expect(formatElapsed(25 * 3_600_000)).toBe('25h 00m')
  })

  it('never renders a negative or unparseable duration', () => {
    expect(formatElapsed(-5)).toBe('0s')
    expect(formatElapsed(Number.NaN)).toBe('0s')
    expect(formatElapsed(null)).toBe('0s')
    expect(formatElapsed(undefined)).toBe('0s')
  })
})

describe('formatCost', () => {
  it('is USD with 2 to 6 decimals (Langfuse)', () => {
    expect(formatCost(0.38)).toBe('$0.38')
    expect(formatCost(0.001234)).toBe('$0.001234')
    expect(formatCost(12)).toBe('$12.00')
    // beyond 6dp it rounds rather than growing
    expect(formatCost(0.0000004)).toBe('$0.00')
  })
  it('treats a missing cost as zero', () => {
    expect(formatCost(null)).toBe('$0.00')
    expect(formatCost(undefined)).toBe('$0.00')
    expect(formatCost(Number.NaN)).toBe('$0.00')
  })
})

describe('formatTokens', () => {
  it('reads in → out with the ∑ aggregate', () => {
    expect(formatTokens({ in: 1234, out: 567 })).toBe('1,234 → 567 (∑ 1,801)')
  })
  it('tolerates partial or absent counts', () => {
    expect(formatTokens({ in: 10 })).toBe('10 → 0 (∑ 10)')
    expect(formatTokens(null)).toBe('0 → 0 (∑ 0)')
  })
  it('compacts a total for the card row', () => {
    expect(formatTokensCompact(412)).toBe('412')
    expect(formatTokensCompact(41_200)).toBe('41.2k')
    expect(formatTokensCompact(41_000)).toBe('41k')
    expect(formatTokensCompact(1_400_000)).toBe('1.4M')
  })
  it('groups plain counts', () => {
    expect(formatCount(1234)).toBe('1,234')
    expect(formatCount(undefined)).toBe('0')
  })
})

describe('agentElapsed', () => {
  it('runs to "now" while the agent is live (Phoenix: open work is drawn to now)', () => {
    expect(agentElapsed(card(), T0 + 83_000)).toBe(83_000)
  })

  it('freezes at finished_at, whatever the clock says', () => {
    const done = card({ status: 'done', finished_at: '2026-09-20T12:02:00.000Z' })
    expect(agentElapsed(done, T0 + 83_000)).toBe(120_000)
    expect(agentElapsed(done, T0 + 10 * 3_600_000)).toBe(120_000)
  })

  it('falls back to the last heartbeat when there is no clock', () => {
    const c = card({ heartbeat_at: '2026-09-20T12:00:30.000Z' })
    expect(agentElapsed(c, 0)).toBe(30_000)
  })

  it('is null for an agent that was never spawned, and never negative', () => {
    expect(agentElapsed(card({ spawned_at: null }), T0)).toBeNull()
    expect(agentElapsed(card(), T0 - 5_000)).toBe(0)
  })
})

describe('agentStalled (90s)', () => {
  it('is the 90s heartbeat threshold', () => {
    expect(STALE_MS).toBe(90_000)
    const c = card({ heartbeat_at: '2026-09-20T12:00:00.000Z' })
    expect(agentStalled(c, T0 + 89_000)).toBe(false)
    expect(agentStalled(c, T0 + 90_000)).toBe(false)
    expect(agentStalled(c, T0 + 90_001)).toBe(true)
  })

  it('a finished or idle agent never stalls', () => {
    const beat = '2026-09-20T12:00:00.000Z'
    expect(agentStalled(card({ heartbeat_at: beat, finished_at: '2026-09-20T12:01:00.000Z' }), T0 + 600_000)).toBe(false)
    expect(agentStalled(card({ heartbeat_at: beat, status: 'done' }), T0 + 600_000)).toBe(false)
    expect(agentStalled(card({ heartbeat_at: beat, status: 'idle' }), T0 + 600_000)).toBe(false)
  })

  it('an agent that has never beaten is not stalled (nothing has been claimed yet)', () => {
    expect(agentStalled(card(), T0 + 600_000)).toBe(false)
  })
})

describe('metrics row', () => {
  it('counts files whether the server sends a list or a number', () => {
    expect(filesTouchedCount({ files_touched: ['a.py', 'b.py'] })).toBe(2)
    expect(filesTouchedCount({ files_touched: 6 })).toBe(6)
    expect(filesTouchedCount({})).toBe(0)
  })

  it('reads as the B.7 ASCII row', () => {
    expect(
      formatMetricsLine({ tool_calls: 14, files_touched: ['a', 'b', 'c', 'd', 'e', 'f'], tokens_in: 33_000, tokens_out: 8_200, cost_usd: 0.38 }),
    ).toBe('14 tools · 6 files · ∑ 41.2k tok · $0.38')
  })
})
