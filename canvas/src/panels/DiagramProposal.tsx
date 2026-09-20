import { useEffect, useMemo, useState } from 'react'
import { store, useStore } from '../state/store'
import { isBoardName } from '../sync/boards'
import { getWiring } from '../sync/wire'
import type { DiagramRequest } from '../state/types'
import { focusBoard } from './BoardSwitcher'
import {
  boardTitle,
  changeSummary,
  diagramChangeLines,
  diagramReplyPayload,
  groupChangeLines,
  shouldGroupChanges,
  type ChangeLine,
} from './diagramChanges'
import { useModalDialog } from './useModalDialog'

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

  /**
   * Esc = Reject WITHOUT sending (B.S6 item 9): the modal closes and the proposal stays pending
   * on the server, so `client.hello` brings it straight back. Nothing is written either way.
   */
  const dismiss = () => {
    if (pending) return
    store.removeDiagramRequest(req.request_id)
    store.note('diagram', `dismissed ${req.name} (req ${req.request_id}) — still pending on the server`)
  }
  const dialogRef = useModalDialog<HTMLDivElement>({ onEscape: dismiss })

  const lines = diagramChangeLines(req)
  const grouped = shouldGroupChanges(lines)
  const groups = useMemo(() => groupChangeLines(lines), [req])
  const summary = changeSummary(req)
  const textId = `wb-mermaid-${req.request_id}`
  const noteId = `wb-note-${req.request_id}`
  const titleId = `wb-diagram-title-${req.request_id}`

  /** B.S6 item 11: pan the camera to the board being proposed, behind the modal */
  const showMe = () => {
    const editor = getWiring()?.editor
    if (editor && isBoardName(req.name)) focusBoard(editor, req.name)
  }

  return (
    <div className="wb-modal-backdrop" onPointerDown={(e) => e.stopPropagation()}>
      <div className="wb-modal wb-diagram" role="dialog" aria-modal="true" aria-labelledby={titleId} tabIndex={-1} ref={dialogRef}>
        <h2 id={titleId}>
          Proposed diagram: {boardTitle(req.name)}
          {isBoardName(req.name) && (
            <button
              type="button"
              className="wb-btn wb-showme"
              title={`Move the canvas to the ${req.name.toUpperCase()} board behind this dialog`}
              aria-label={`Show me the ${req.name.toUpperCase()} board`}
              onClick={showMe}
            >
              Show me
            </button>
          )}
        </h2>
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

        <h3>Changes{lines.length ? ` (${lines.length})` : ''}</h3>
        <div className={`wb-changes${grouped ? ' grouped' : ''}`} data-testid="diagram-changes">
          {lines.length === 0 && <div className="wb-change none">no changes — the board already matches this diagram</div>}
          {lines.length > 0 &&
            (grouped
              ? groups.map((g) => <ChangeGroupBlock key={g.key} title={g.title} tone={g.tone} lines={g.lines} defaultOpen={g.defaultOpen} />)
              : lines.map((l) => <ChangeRow key={l.key} line={l} />))}
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
          <span className="wb-queue wb-esc-hint">Esc closes this and leaves the proposal pending</span>
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

function ChangeRow({ line }: { line: ChangeLine }) {
  return (
    <div className={`wb-change ${line.tone}`}>
      <span className="sign" aria-hidden="true">
        {line.sign}
      </span>
      <code>{line.text}</code>
      {/* the spoken half of the +/− encoding: colour is never the only signal (B.1 item 12) */}
      <span className="what">
        {line.tone} {line.what}
      </span>
      {line.detail && <span className="detail">{line.detail}</span>}
    </div>
  )
}

/** one collapsible bucket of a large diff (B.S6 item 10) */
function ChangeGroupBlock({
  title,
  tone,
  lines,
  defaultOpen,
}: {
  title: string
  tone: 'added' | 'removed'
  lines: ChangeLine[]
  defaultOpen: boolean
}) {
  const [open, setOpen] = useState(defaultOpen)
  return (
    <div className={`wb-change-group ${tone}`}>
      <button
        type="button"
        className="wb-change-head"
        aria-expanded={open}
        title={`${open ? 'Collapse' : 'Expand'} ${title}`}
        aria-label={`${open ? 'Collapse' : 'Expand'} ${title}`}
        onClick={() => setOpen((v) => !v)}
      >
        <span aria-hidden="true">{open ? '▾' : '▸'}</span>
        <span className="sign" aria-hidden="true">
          {tone === 'added' ? '+' : '−'}
        </span>
        <span>{title}</span>
      </button>
      {open && lines.map((l) => <ChangeRow key={l.key} line={l} />)}
    </div>
  )
}
