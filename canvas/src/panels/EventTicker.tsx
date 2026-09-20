// Event feed island, top left (plan B.8): the retained RAW events, newest first, with error rows
// tinted `--wb-blocked` (B.1 item 9 — the failing path is lit up).
//
// v1 read the flattened `ticker`; v2 reads `store.events`, which keeps `agent_id`, `node_id` and
// `data` so a row can say who did what where. The ticker is still the fallback before the first
// event arrives (local notes such as "sent renamed (seq 12)" live only there).
import { useState } from 'react'
import { useStore } from '../state/store'
import { isErrorEvent } from './AgentInspector'

const SHOW = 30

export function EventTicker() {
  const events = useStore((s) => s.events)
  const ticker = useStore((s) => s.ticker)
  const lastError = useStore((s) => s.lastError)
  const [open, setOpen] = useState(true)
  const items = events.slice(-SHOW).reverse()
  const notes = events.length === 0 ? ticker.slice(-SHOW).reverse() : []

  return (
    <div className="wb-panel wb-ticker" data-testid="event-ticker">
      <div className="wb-ticker-head" onClick={() => setOpen((o) => !o)}>
        <span>Events {events.length ? `(${events.length})` : ''}</span>
        <span>{open ? '▾' : '▸'}</span>
      </div>
      {open && (
        <ul className="wb-ticker-list">
          {lastError && (
            <li className="err" title={`server error, seq ${lastError.seq}`}>
              <span className="k">error</span>
              {lastError.code}: {lastError.message}
            </li>
          )}
          {items.length === 0 && notes.length === 0 && <li className="wb-ticker-empty">waiting for the server…</li>}
          {items.map((ev) => (
            <li
              key={`${ev.seq}-${ev.ts}`}
              className={isErrorEvent(ev) ? 'err' : undefined}
              title={`${ev.ts} · ${ev.agent_id}${ev.node_id ? ` · ${ev.node_id}` : ''}`}
            >
              <span className="seq">#{ev.seq}</span>
              <span className="k">{ev.type}</span>
              <span className="who">{ev.agent_id}</span>
              {ev.note}
              {ev.node_id && <span className="where"> · {ev.node_id}</span>}
            </li>
          ))}
          {notes.map((e, i) => (
            <li key={`n-${i}-${e.ts}`} title={e.ts}>
              <span className="k">{e.kind}</span>
              {e.text}
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}
