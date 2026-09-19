import { useState } from 'react'
import { store, useStore } from '../state/store'
import { getWiring } from '../sync/wire'

export function ChatBox() {
  const selectedAgentId = useStore((s) => s.selectedAgentId)
  const socket = useStore((s) => s.socket)
  const [text, setText] = useState('')

  const send = () => {
    const t = text.trim()
    const w = getWiring()
    if (!t || !w) return
    w.socket.send('chat.message', selectedAgentId ? { text: t, agentId: selectedAgentId } : { text: t })
    store.note('chat', `you → ${selectedAgentId ?? 'root'}: ${t}`)
    setText('')
  }

  return (
    <div className="wb-panel wb-chat" data-testid="chat-box">
      <div className="wb-chat-to">
        <span>to:</span>
        {selectedAgentId ? (
          <>
            <span className="agent">{selectedAgentId}</span>
            <span>(selected)</span>
            <button className="wb-btn" onClick={() => store.selectAgent(null)} title="clear agent selection">
              clear
            </button>
          </>
        ) : (
          <span className="agent">root</span>
        )}
      </div>
      <textarea
        className="wb-textarea"
        rows={3}
        placeholder={selectedAgentId ? `Message ${selectedAgentId}…` : 'Message the root session…'}
        value={text}
        onChange={(e) => setText(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
            e.preventDefault()
            send()
          }
          e.stopPropagation()
        }}
      />
      <div className="wb-chat-row">
        <span className="wb-status">{socket === 'open' ? '' : `socket ${socket}: queued until reconnect`}</span>
        <button className="wb-btn primary" onClick={send} disabled={!text.trim()}>
          Send
        </button>
      </div>
    </div>
  )
}
