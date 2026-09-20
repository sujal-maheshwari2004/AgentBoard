import { useState } from 'react'
import { store, useStore } from '../state/store'
import { getWiring } from '../sync/wire'
import { useModalDialog } from './useModalDialog'

/**
 * `risky_edit.request` → summary + diff; approve needs an explanation → `risky_edit.reply`.
 * Reject sends approved:false and resyncs the canvas from the last server truth (the server's
 * `edit.reject` / re-broadcast is authoritative; the local resync just removes the lag).
 */
export function RiskyEditConfirm() {
  const risky = useStore((s) => s.riskyEdits)
  const r = risky[0]
  if (!r) return null
  return <RiskyDialog key={r.request_id} requestId={r.request_id} summary={r.summary} diff={r.diff} affected={r.affected} remaining={risky.length - 1} />
}

function RiskyDialog(props: { requestId: string; summary: string; diff: string; affected: string[]; remaining: number }) {
  const [note, setNote] = useState('')
  // focus trap only: the most destructive modal must not be dismissable by a stray Esc (B.S6 #9)
  const dialogRef = useModalDialog<HTMLDivElement>()
  const reply = (approved: boolean) => {
    const w = getWiring()
    if (!w) return
    w.socket.send('risky_edit.reply', { request_id: props.requestId, approved, note: note.trim() })
    store.note('risky_edit', `${approved ? 'accepted' : 'rejected'}: ${props.summary}`)
    store.removeRiskyEdit(props.requestId)
    if (!approved) w.resync()
  }
  return (
    <div className="wb-modal-backdrop" onPointerDown={(e) => e.stopPropagation()}>
      <div className="wb-modal" role="dialog" aria-modal="true" aria-label="Risky edit" tabIndex={-1} ref={dialogRef}>
        <h2>Risky edit — confirm</h2>
        <div className="meta">
          {props.remaining > 0 && <span className="wb-queue">{props.remaining} more queued · </span>}
          request <code>{props.requestId}</code>
        </div>
        <p style={{ whiteSpace: 'pre-wrap' }}>{props.summary}</p>
        {props.affected.length > 0 && (
          <div className="wb-affected" style={{ marginBottom: 8 }}>
            affects:{' '}
            {props.affected.map((id) => (
              <code key={id}>{id}</code>
            ))}
          </div>
        )}
        {props.diff && <pre>{props.diff}</pre>}
        <h3>Why? (required to approve)</h3>
        <textarea
          className="wb-textarea"
          rows={3}
          autoFocus
          value={note}
          placeholder="Explain the change for the root session and the agents it affects"
          onChange={(e) => setNote(e.target.value)}
          onKeyDown={(e) => e.stopPropagation()}
        />
        <div className="actions">
          <button className="wb-btn danger" onClick={() => reply(false)}>
            Reject (revert)
          </button>
          <button className="wb-btn primary" disabled={!note.trim()} onClick={() => reply(true)}>
            Approve
          </button>
        </div>
      </div>
    </div>
  )
}
