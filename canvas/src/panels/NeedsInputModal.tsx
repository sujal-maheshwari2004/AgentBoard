import { useEffect, useState } from 'react'
import { store, useStore } from '../state/store'
import { getWiring } from '../sync/wire'
import type { NeedsInputPrompt } from '../state/types'
import { useModalDialog } from './useModalDialog'

/** Queue of `needs_input` prompts; one shown at a time. */
export function NeedsInputModal() {
  const prompts = useStore((s) => s.prompts)
  const dispatches = useStore((s) => s.dispatches)
  const risky = useStore((s) => s.riskyEdits)
  const diagrams = useStore((s) => s.diagramRequests)
  // B.8 precedence: risky edit → diagram proposal → dispatch → needs_input
  if (dispatches.length || risky.length || diagrams.length) return null
  const p = prompts[0]
  if (!p) return null
  return <PromptDialog key={p.prompt_id} prompt={p} remaining={prompts.length - 1} />
}

function PromptDialog({ prompt, remaining }: { prompt: NeedsInputPrompt; remaining: number }) {
  const [text, setText] = useState('')
  useEffect(() => setText(''), [prompt.prompt_id])
  // focus trap only: a prompt has no "dismiss" — the agent is waiting for an answer (B.S6 #9)
  const dialogRef = useModalDialog<HTMLDivElement>()

  const reply = (value: unknown) => {
    const w = getWiring()
    if (!w) return
    w.socket.send('prompt.reply', { prompt_id: prompt.prompt_id, value })
    store.note('reply', `reply to ${prompt.agent_id}: ${String(value)}`)
    store.removePrompt(prompt.prompt_id)
  }

  return (
    <div className="wb-modal-backdrop" onPointerDown={(e) => e.stopPropagation()}>
      <div className="wb-modal" role="dialog" aria-modal="true" aria-label="Agent needs input" tabIndex={-1} ref={dialogRef}>
        <h2>{prompt.agent_id} needs input</h2>
        <div className="meta">
          {prompt.node_id ? `on ${prompt.node_id} · ` : ''}
          {prompt.kind}
          {remaining > 0 && <span className="wb-queue"> · {remaining} more queued</span>}
        </div>
        <p style={{ whiteSpace: 'pre-wrap' }}>{prompt.question}</p>
        {prompt.kind === 'choice' && (
          <div className="wb-choices">
            {prompt.choices.map((c) => (
              <button key={c} className="wb-btn" onClick={() => reply(c)}>
                {c}
              </button>
            ))}
          </div>
        )}
        {prompt.kind === 'confirm' && (
          <div className="actions">
            <button className="wb-btn" onClick={() => reply(false)}>
              No
            </button>
            <button className="wb-btn primary" onClick={() => reply(true)}>
              Yes
            </button>
          </div>
        )}
        {prompt.kind === 'text' && (
          <>
            <textarea
              className="wb-textarea"
              rows={4}
              autoFocus
              value={text}
              onChange={(e) => setText(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter' && (e.metaKey || e.ctrlKey) && text.trim()) reply(text.trim())
                e.stopPropagation()
              }}
            />
            <div className="actions">
              <button className="wb-btn primary" disabled={!text.trim()} onClick={() => reply(text.trim())}>
                Reply
              </button>
            </div>
          </>
        )}
      </div>
    </div>
  )
}
