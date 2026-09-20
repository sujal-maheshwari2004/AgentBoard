import { useState } from 'react'
import { store, useStore } from '../state/store'
import { getWiring } from '../sync/wire'

/** `dispatch.request` → Approve/Reject with an optional note → `dispatch.reply`. */
export function DispatchPopup() {
  const dispatches = useStore((s) => s.dispatches)
  const risky = useStore((s) => s.riskyEdits)
  const diagrams = useStore((s) => s.diagramRequests)
  // B.8 precedence: risky edit → diagram proposal → dispatch → needs_input
  if (risky.length || diagrams.length) return null
  const d = dispatches[0]
  if (!d) return null
  return <DispatchDialog key={d.request_id} requestId={d.request_id} nodeId={d.node_id} agentId={d.agent_id} jobSpec={d.job_spec_md} remaining={dispatches.length - 1} />
}

function DispatchDialog(props: { requestId: string; nodeId: string; agentId: string; jobSpec: string; remaining: number }) {
  const [note, setNote] = useState('')
  const node = useStore((s) => s.nodes[props.nodeId])
  const reply = (approved: boolean) => {
    const w = getWiring()
    if (!w) return
    const n = note.trim()
    w.socket.send('dispatch.reply', n ? { request_id: props.requestId, approved, note: n } : { request_id: props.requestId, approved })
    store.note('dispatch', `${approved ? 'approved' : 'rejected'} ${props.nodeId} → ${props.agentId}`)
    store.removeDispatch(props.requestId)
  }
  return (
    <div className="wb-modal-backdrop" onPointerDown={(e) => e.stopPropagation()}>
      <div className="wb-modal" role="dialog" aria-label="Dispatch request">
        <h2>Dispatch {props.agentId}?</h2>
        <div className="meta">
          node <code>{props.nodeId}</code>
          {node ? ` — ${node.title} (${node.status})` : ''} · agent <code>{props.agentId}</code>
          {props.remaining > 0 && <span className="wb-queue"> · {props.remaining} more queued</span>}
        </div>
        <h3>Job spec</h3>
        <pre>{props.jobSpec}</pre>
        <textarea
          className="wb-textarea"
          rows={2}
          placeholder="Optional note for the root session"
          value={note}
          onChange={(e) => setNote(e.target.value)}
          onKeyDown={(e) => e.stopPropagation()}
        />
        <div className="actions">
          <button className="wb-btn danger" onClick={() => reply(false)}>
            Reject
          </button>
          <button className="wb-btn primary" onClick={() => reply(true)}>
            Approve
          </button>
        </div>
      </div>
    </div>
  )
}
