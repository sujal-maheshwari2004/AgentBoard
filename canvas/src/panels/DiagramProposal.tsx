import { useEffect, useState } from 'react'
import { store, useStore } from '../state/store'
import { getWiring } from '../sync/wire'
import type { DiagramRequest } from '../state/types'
import { boardTitle, changeSummary, diagramChangeLines, diagramReplyPayload } from './diagramChanges'

/**
 * B.10 — root proposes a board, the owner approves it. Nothing reaches `.whiteboard/plan/`
 * until Approve is pressed here, so this modal is the only write path for a diagram.
 *
 * Precedence (B.8): risky edit → **diagram proposal** → dispatch → needs_input.
 */
export function DiagramProposal() {
  const risky = useStore((s) => s.riskyEdits)
  const requests = useStore((s) => s.diagramRequests)
  // the risky-edit confirm is more destructive and takes the screen first
  if (risky.length) return null
  const req = requests[0]
  if (!req) return null
  return <DiagramDialog key={req.request_id} req={req} remaining={requests.length - 1} />
}

/**
 * Owner edits survive a remount (another modal taking precedence, a re-sent proposal): the
 * textarea is the only place that text exists until the server accepts it.
 */
const drafts = new Map<string, { text: string; note: string }>()

/** errors that mean the proposal is no longer pending server-side — nothing left to approve */
const GONE = new Set(['unknown_request', 'already_resolved'])

function DiagramDialog({ req, remaining }: { req: DiagramRequest; remaining: number }) {
  const draft = drafts.get(req.request_id)
  const [text, setText] = useState(draft?.text ?? req.mermaid)
  const [note, setNote] = useState(draft?.note ?? '')
  /** the seq of the `diagram.reply` in flight; `edit.ack`/`server.error` come back on it */
  const [sentSeq, setSentSeq] = useState<number | null>(null)
  const lastAck = useStore((s) => s.lastAck)
  const lastError = useStore((s) => s.lastError)

  const remember = (t: string, n: string) => drafts.set(req.request_id, { text: t, note: n })

  // The reply is correlated by send seq, not by request id: the server answers `diagram.reply`
  // with `edit.ack {forSeq}` on success and `server.error {forSeq, code}` on a bad diagram,
  // and on `bad_diagram` it deliberately leaves the proposal pending (A.2 `on_diagram_reply`).
  const failure = sentSeq !== null && lastError?.forSeq === sentSeq ? lastError : null
  const pending = sentSeq !== null && !failure
  const errorText = failure?.message ?? (req.error || '')

  useEffect(() => {
    if (sentSeq === null) return
    const settled = lastAck?.forSeq === sentSeq || (failure !== null && GONE.has(failure.code))
    if (!settled) return
    drafts.delete(req.request_id)
    store.removeDiagramRequest(req.request_id)
  }, [sentSeq, lastAck, failure, req.request_id])

  const reply = (approved: boolean) => {
    const w = getWiring()
    if (!w) return
    const payload = diagramReplyPayload(req, { approved, note, text })
    setSentSeq(w.replyDiagram(payload))
    store.note('diagram', `${approved ? 'approved' : 'rejected'} ${req.name} (req ${req.request_id})${payload.mermaid ? ' with edits' : ''}`)
  }

  const lines = diagramChangeLines(req)
  const summary = changeSummary(req)
  const textId = `wb-mermaid-${req.request_id}`
  const noteId = `wb-note-${req.request_id}`

  return (
    <div className="wb-modal-backdrop" onPointerDown={(e) => e.stopPropagation()}>
      <div className="wb-modal wb-diagram" role="dialog" aria-label="Diagram proposal">
        <h2>Proposed diagram: {boardTitle(req.name)}</h2>
        <div className="meta">
          board <code>{req.name}</code> · request <code>{req.request_id}</code>
          {summary ? ` · ${summary}` : ''}
          {remaining > 0 && <span className="wb-queue"> · {remaining} more queued</span>}
        </div>
        {req.rationale && (
          <p className="wb-why">
            <span className="lead">Why:</span> {req.rationale}
          </p>
        )}

        <h3>Changes</h3>
        <div className="wb-changes">
          {lines.length === 0 ? (
            <div className="wb-change none">no changes — the board already matches this diagram</div>
          ) : (
            lines.map((l) => (
              <div key={l.key} className={`wb-change ${l.tone}`}>
                <span className="sign" aria-hidden="true">
                  {l.sign}
                </span>
                <code>{l.text}</code>
                <span className="what">
                  {l.tone} {l.what}
                </span>
                {l.detail && <span className="detail">{l.detail}</span>}
              </div>
            ))
          )}
        </div>

        {errorText && (
          <div className="wb-diagram-error" role="alert">
            {errorText}
          </div>
        )}

        <h3>
          <label htmlFor={textId}>Mermaid (editable)</label>
        </h3>
        <textarea
          id={textId}
          className="wb-textarea wb-mermaid"
          rows={14}
          spellCheck={false}
          value={text}
          onChange={(e) => {
            setText(e.target.value)
            remember(e.target.value, note)
          }}
          // tldraw's shortcuts live on the document: without this, typing `d` picks the draw tool
          onKeyDown={(e) => e.stopPropagation()}
        />

        <label className="wb-note-row" htmlFor={noteId}>
          <span>Note</span>
          <input
            id={noteId}
            className="wb-input"
            value={note}
            placeholder="Optional — goes to the root session with your verdict"
            onChange={(e) => {
              setNote(e.target.value)
              remember(text, e.target.value)
            }}
            onKeyDown={(e) => e.stopPropagation()}
          />
        </label>

        <div className="actions">
          {pending && <span className="wb-queue">sending…</span>}
          <button className="wb-btn danger" disabled={pending} onClick={() => reply(false)}>
            Reject
          </button>
          <button className="wb-btn primary" disabled={pending || !text.trim()} onClick={() => reply(true)}>
            Approve
          </button>
        </div>
      </div>
    </div>
  )
}
