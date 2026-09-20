// Threaded chat island (plan B.8/B.9), replacing v1's one-way `ChatBox`.
//
//   ┌ root · parser● ─────────────────┐   thread tabs, `root` first, unread dot + count
//   │ root      can we merge parser…  │   sender label in the sender's colour, 11px stamps
//   │                you  yes ─────── │   the human's own lines are right-aligned
//   │ ↩ root: can we merge…           │   a `reply_to` renders as a one-line quoted stub
//   │ [ message agent-parser… ] [Send]│   composer → `chat.message {text, agentId, thread, reply_to}`
//   └─────────────────────────────────┘
//
// Everything on screen comes from `store.threads`, which is fed by `chat.message` alone and
// deduped by `id` — never from `event.append` (A.4). Collapsed, the island is a 44px pill
// carrying the total unread count.
import { useEffect, useMemo, useRef, useState } from 'react'
import { store, useStore } from '../state/store'
import type { ChatMessage } from '../state/types'
import {
  ROOT_THREAD,
  deriveThreads,
  formatClock,
  groupMessages,
  quotedStub,
  senderColor,
  totalUnread,
} from '../state/threads'
import { getWiring } from '../sync/wire'

export function ChatPanel() {
  const threads = useStore((s) => s.threads)
  const unread = useStore((s) => s.threadUnread)
  const agents = useStore((s) => s.agents)
  const activeThread = useStore((s) => s.activeThread)
  const open = useStore((s) => s.chatOpen)
  const socket = useStore((s) => s.socket)
  const [text, setText] = useState('')
  const [replyTo, setReplyTo] = useState<ChatMessage | null>(null)
  const listRef = useRef<HTMLDivElement | null>(null)

  const tabs = useMemo(() => deriveThreads(threads, unread, agents, activeThread), [threads, unread, agents, activeThread])
  const messages = threads[activeThread] ?? EMPTY
  const groups = useMemo(() => groupMessages(messages), [messages])
  const pending = totalUnread(unread)

  // a reply target only makes sense inside the thread it was picked in
  useEffect(() => setReplyTo(null), [activeThread])
  useEffect(() => {
    const el = listRef.current
    if (el) el.scrollTop = el.scrollHeight
  }, [messages.length, activeThread, open])

  if (!open) {
    return (
      <button
        type="button"
        className="wb-island wb-chat-pill"
        data-testid="chat-pill"
        title={pending ? `${pending} unread chat message(s)` : 'Open chat'}
        aria-label={pending ? `Open chat, ${pending} unread` : 'Open chat'}
        onClick={() => store.setChatOpen(true)}
      >
        <span aria-hidden="true">💬</span>
        {pending > 0 && <span className="wb-chat-count">{pending > 99 ? '99+' : pending}</span>}
      </button>
    )
  }

  const send = () => {
    const t = text.trim()
    const wiring = getWiring()
    if (!t || !wiring) return
    wiring.sendChat(t, activeThread, replyTo?.id)
    setText('')
    setReplyTo(null)
  }

  return (
    <div className="wb-panel wb-chat" data-testid="chat-panel">
      <div className="wb-chat-tabs" role="tablist" aria-label="Chat threads">
        {tabs.map((t) => (
          <button
            key={t.thread}
            type="button"
            role="tab"
            aria-selected={t.thread === activeThread}
            className={`wb-chat-tab${t.thread === activeThread ? ' active' : ''}`}
            title={`${t.thread} — ${t.count} message(s)${t.unread ? `, ${t.unread} unread` : ''}${t.live ? ', live' : ''}`}
            onClick={() => store.setActiveThread(t.thread)}
          >
            {t.label}
            {t.unread > 0 && (
              <span className="wb-chat-dot" aria-label={`${t.unread} unread`}>
                {t.unread > 9 ? '9+' : t.unread}
              </span>
            )}
          </button>
        ))}
        <span className="wb-chat-spacer" />
        <button
          type="button"
          className="wb-chat-collapse"
          title="Collapse chat"
          aria-label="Collapse chat"
          onClick={() => store.setChatOpen(false)}
        >
          ▾
        </button>
      </div>

      <div className="wb-chat-list" ref={listRef} data-testid="chat-list">
        {messages.length === 0 && (
          <p className="wb-chat-empty">
            {activeThread === ROOT_THREAD ? 'No messages yet — say something to the root session.' : `No messages with ${activeThread} yet.`}
          </p>
        )}
        {groups.map((g) => (
          <div key={`${g.from}-${g.messages[0].id}`} className={`wb-chat-group${g.mine ? ' mine' : ''}`}>
            {!g.mine && (
              <span className="wb-chat-from" style={{ color: senderColor(g.from, agents) }}>
                {g.from}
              </span>
            )}
            {g.messages.map((m) => {
              const stub = quotedStub(messages, m.reply_to)
              return (
                <div key={m.id} className="wb-chat-msg">
                  {stub && <div className="wb-chat-quote">{stub}</div>}
                  <div className="wb-chat-bubble" onDoubleClick={() => setReplyTo(m)} title="double-click to reply">
                    {m.text}
                  </div>
                  <span className="wb-chat-ts" title={m.ts}>
                    {formatClock(m.ts)}
                  </span>
                </div>
              )
            })}
          </div>
        ))}
      </div>

      {replyTo && (
        <div className="wb-chat-replying">
          <span>{quotedStub(messages, replyTo.id, 40)}</span>
          <button type="button" className="wb-chat-collapse" onClick={() => setReplyTo(null)} aria-label="Cancel reply">
            ✕
          </button>
        </div>
      )}

      <div className="wb-chat-composer">
        <textarea
          className="wb-textarea"
          rows={2}
          placeholder={activeThread === ROOT_THREAD ? 'Message the root session…' : `Message ${activeThread}…`}
          aria-label={`Message ${activeThread}`}
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
          <span className="wb-status">{socket === 'open' ? '⌘↵ to send' : `socket ${socket}: queued until reconnect`}</span>
          <button className="wb-btn primary" onClick={send} disabled={!text.trim()}>
            Send
          </button>
        </div>
      </div>
    </div>
  )
}

const EMPTY: ChatMessage[] = []
