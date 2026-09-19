import { useState } from 'react'
import { useStore } from '../state/store'

const SHOW = 20

/** Top-left: last 20 events, collapsible. */
export function EventTicker() {
  const ticker = useStore((s) => s.ticker)
  const [open, setOpen] = useState(true)
  const items = ticker.slice(-SHOW).reverse()
  return (
    <div className="wb-panel wb-ticker" data-testid="event-ticker">
      <div className="wb-ticker-head" onClick={() => setOpen((o) => !o)}>
        <span>
          Events {ticker.length ? `(${ticker.length})` : ''}
        </span>
        <span>{open ? '▾' : '▸'}</span>
      </div>
      {open && (
        <ul className="wb-ticker-list">
          {items.length === 0 && <li style={{ color: '#9aa0a6' }}>waiting for the server…</li>}
          {items.map((e, i) => (
            <li key={`${e.seq}-${e.ts}-${i}`} title={e.ts}>
              {e.seq > 0 && <span className="seq">#{e.seq}</span>}
              <span className="k">{e.kind}</span>
              {e.text}
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}
