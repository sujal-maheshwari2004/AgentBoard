import { useState } from 'react'
import { store } from '../state/store'
import { getWiring } from '../sync/wire'
import { useModalDialog } from './useModalDialog'

export function PastePlanButton() {
  const [open, setOpen] = useState(false)
  return (
    <>
      <button className="wb-btn" onClick={() => setOpen(true)} title="Paste a mermaid flowchart, frontmatter markdown or free text">
        Paste plan
      </button>
      {open && <PastePlanModal onClose={() => setOpen(false)} />}
    </>
  )
}

export function PastePlanModal({ onClose }: { onClose: () => void }) {
  const [text, setText] = useState('')
  // Esc closes it: nothing has been sent yet (B.S6 #9)
  const dialogRef = useModalDialog<HTMLDivElement>({ onEscape: onClose })
  const submit = () => {
    const t = text.trim()
    const w = getWiring()
    if (!t || !w) return
    w.socket.send('plan.paste', { text: t })
    store.note('paste', `plan pasted (${t.length} chars)`)
    onClose()
  }
  return (
    <div className="wb-modal-backdrop" onPointerDown={(e) => e.stopPropagation()}>
      <div className="wb-modal" role="dialog" aria-modal="true" aria-label="Paste plan" tabIndex={-1} ref={dialogRef}>
        <h2>Paste plan</h2>
        <div className="meta">
          A <code>flowchart</code> mermaid block or frontmatter markdown is ingested directly; anything else is logged and
          handed to the root session to structure.
        </div>
        <textarea
          className="wb-textarea"
          rows={14}
          autoFocus
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => e.stopPropagation()}
          placeholder={'flowchart TD\n    node-a[First step]\n    node-b[Second step]\n    node-a --> node-b'}
        />
        <div className="actions">
          <button className="wb-btn" onClick={onClose}>
            Cancel
          </button>
          <button className="wb-btn primary" onClick={submit} disabled={!text.trim()}>
            Send to whiteboard
          </button>
        </div>
      </div>
    </div>
  )
}
