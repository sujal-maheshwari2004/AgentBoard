// Pure half of the event feed (B.S6 item 1).
//
// The feed used to open at 320 x 45vh in the top-left corner, directly on top of the first board
// frame: on load the owner saw a log, not their plan. It now starts COLLAPSED as a compact header
// pill carrying the event count and the most recent line, and expands on click.
import type { PlanEvent } from '../state/types'
import type { TickerEntry } from '../state/store'

/** the feed is collapsed on load — the board is the thing worth seeing first */
export const FEED_DEFAULT_OPEN = false

/** how many rows the expanded feed shows */
export const FEED_SHOW = 30

export interface FeedSummary {
  /** how many raw events are retained (0 before the first one arrives) */
  count: number
  /** what the collapsed pill says after "Events" */
  text: string
  /** `err` tints the pill with `--wb-blocked`: an error is the one thing worth colouring shut */
  tone: 'err' | 'plain'
  /** the `title` / `aria-label` the pill carries (B.1 item 12) */
  label: string
}

function describe(ev: PlanEvent): string {
  return `${ev.agent_id} ${ev.type}${ev.note ? `: ${ev.note}` : ''}`
}

function clip(text: string, max = 48): string {
  return text.length > max ? `${text.slice(0, max - 1)}…` : text
}

/**
 * What the collapsed pill reads. A server error wins over the newest event (the failing path is
 * lit up, B.1 item 9); otherwise the most recent event, else the most recent local note, else
 * "waiting for the server".
 */
export function feedSummary(
  events: readonly PlanEvent[],
  ticker: readonly TickerEntry[] = [],
  lastError: { code: string; message: string } | null = null,
): FeedSummary {
  const count = events.length
  if (lastError) {
    return {
      count,
      text: clip(`${lastError.code}: ${lastError.message}`),
      tone: 'err',
      label: `Events (${count}) — last: server error ${lastError.code}: ${lastError.message}. Click to expand the event feed`,
    }
  }
  const last = events.length ? events[events.length - 1] : null
  if (last) {
    const line = describe(last)
    return {
      count,
      text: clip(line),
      tone: 'plain',
      label: `Events (${count}) — last: ${line}. Click to expand the event feed`,
    }
  }
  const note = ticker.length ? ticker[ticker.length - 1] : null
  if (note) {
    return {
      count,
      text: clip(`${note.kind} ${note.text}`),
      tone: 'plain',
      label: `Events (0) — last: ${note.kind} ${note.text}. Click to expand the event feed`,
    }
  }
  return { count: 0, text: 'waiting for the server…', tone: 'plain', label: 'Event feed — waiting for the server. Click to expand' }
}
