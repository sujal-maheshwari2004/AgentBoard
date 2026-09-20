// B.9 / B.11: chat threads in the store and the pure helpers around them.
//
// The one behaviour everything else hangs off: `client.hello` replays the last 50 `chat.message`s
// per thread BEFORE the event replay, so on every reconnect backfill overlaps live traffic. A
// message is therefore identified by `id`, ordered by `seq`, and never built from `event.append`.
import { describe, expect, it } from 'vitest'
import { createStore, initialState, type StoreState } from '../src/state/store'
import {
  ROOT_THREAD,
  chatSendPayload,
  clearUnread,
  deriveThreads,
  formatClock,
  groupMessages,
  mergeChatMessage,
  quotedStub,
  senderColor,
  threadOf,
  totalUnread,
} from '../src/state/threads'
import type { AgentCard, ChatMessage, ServerMessage } from '../src/state/types'

function chat(seq: number, thread: string, from: string, text: string, extra: Partial<ChatMessage> = {}): ChatMessage {
  return {
    id: `chat-${seq}`,
    thread,
    from,
    to: from === 'user' ? (thread === ROOT_THREAD ? 'root' : thread) : 'user',
    text,
    ts: `2026-09-20T00:0${seq % 10}:00.000Z`,
    seq,
    reply_to: null,
    node_id: null,
    ...extra,
  }
}

let counter = 500
function msg(payload: ChatMessage): ServerMessage {
  return { type: 'chat.message', payload, seq: ++counter, ts: '2026-09-20T00:00:00.000Z', replyTo: null }
}

function agent(id: string, status: AgentCard['status'], finished?: string): AgentCard {
  return { id, assigned_node: null, status, ready_deps: [], finished_at: finished ?? null }
}

// ---------- merge / dedupe ----------

describe('mergeChatMessage', () => {
  it('appends live traffic in arrival order', () => {
    let list = mergeChatMessage(undefined, chat(1, 'root', 'user', 'one'))!
    list = mergeChatMessage(list, chat(2, 'root', 'root', 'two'))!
    expect(list.map((m) => m.seq)).toEqual([1, 2])
  })

  it('returns null for an id it already has, whatever else changed', () => {
    const list = mergeChatMessage(undefined, chat(1, 'root', 'user', 'one'))!
    expect(mergeChatMessage(list, chat(1, 'root', 'user', 'one'))).toBeNull()
    // the same id with different text is still the same message: the server is authoritative
    expect(mergeChatMessage(list, { ...chat(1, 'root', 'user', 'edited'), seq: 9 })).toBeNull()
  })

  it('inserts an out-of-order message at its seq position', () => {
    let list = mergeChatMessage(undefined, chat(4, 'root', 'root', 'four'))!
    list = mergeChatMessage(list, chat(8, 'root', 'root', 'eight'))!
    list = mergeChatMessage(list, chat(6, 'root', 'user', 'six'))!
    list = mergeChatMessage(list, chat(1, 'root', 'user', 'one'))!
    expect(list.map((m) => m.seq)).toEqual([1, 4, 6, 8])
  })
})

// ---------- the reducer: backfill ⨯ live ----------

describe('the chat.message reducer case', () => {
  it('merges a hello backfill that overlaps live traffic, by id and in seq order', () => {
    const s = createStore()
    // live traffic seen before the socket dropped
    s.dispatch(msg(chat(10, 'root', 'user', 'can we merge parser and files?')))
    s.dispatch(msg(chat(11, 'root', 'root', 'yes — one node')))
    // reconnect: hello replays the last 50 per thread, three of which we already have
    for (const seq of [9, 10, 11, 12]) s.dispatch(msg(chat(seq, 'root', seq % 2 ? 'root' : 'user', `m${seq}`)))
    const thread = s.getState().threads.root
    expect(thread.map((m) => m.seq)).toEqual([9, 10, 11, 12])
    expect(thread.map((m) => m.id)).toEqual(['chat-9', 'chat-10', 'chat-11', 'chat-12'])
    // the ones we already had keep their original text: a duplicate id changes nothing
    expect(thread[1].text).toBe('can we merge parser and files?')
  })

  it('returns the very same state object for a duplicate (no re-render on reconnect)', () => {
    const s = createStore()
    s.dispatch(msg(chat(1, 'root', 'user', 'hi')))
    const before = s.getState()
    s.dispatch(msg(chat(1, 'root', 'user', 'hi')))
    expect(s.getState()).toBe(before)
  })

  it('keeps threads apart and never reads event.append', () => {
    const s = createStore()
    s.dispatch(msg(chat(1, 'root', 'user', 'root line')))
    s.dispatch(msg(chat(2, 'agent-parser', 'user', 'use the fixture corpus')))
    s.dispatch(msg(chat(3, 'agent-parser', 'agent-parser', 'will do')))
    s.dispatch({
      type: 'event.append',
      payload: { seq: 4, ts: 't', agent_id: 'user', node_id: null, type: 'chat', note: 'a chat EVENT', data: { thread: 'root' } },
      seq: 4,
      ts: 't',
      replyTo: null,
    })
    const st = s.getState()
    expect(Object.keys(st.threads).sort()).toEqual(['agent-parser', 'root'])
    expect(st.threads.root).toHaveLength(1)
    expect(st.threads['agent-parser'].map((m) => m.from)).toEqual(['user', 'agent-parser'])
    // the event is retained raw for the feeds but contributes nothing to any thread
    expect(st.events).toHaveLength(1)
  })

  it('treats a missing thread as root', () => {
    const s = createStore()
    s.dispatch(msg({ ...chat(1, '', 'root', 'orphan') }))
    expect(s.getState().threads.root.map((m) => m.text)).toEqual(['orphan'])
    expect(threadOf({ thread: '  ' })).toBe(ROOT_THREAD)
  })
})

// ---------- unread ----------

describe('unread counting', () => {
  it('counts only threads that are not the active one, and clears on open', () => {
    const s = createStore()
    s.dispatch(msg(chat(1, 'root', 'root', 'to root')))
    s.dispatch(msg(chat(2, 'agent-parser', 'agent-parser', 'a')))
    s.dispatch(msg(chat(3, 'agent-parser', 'agent-parser', 'b')))
    s.dispatch(msg(chat(4, 'agent-files', 'agent-files', 'c')))
    expect(s.getState().threadUnread).toEqual({ 'agent-parser': 2, 'agent-files': 1 })
    expect(totalUnread(s.getState().threadUnread)).toBe(3)

    s.setActiveThread('agent-parser')
    expect(s.getState().threadUnread).toEqual({ 'agent-files': 1 })
    // while a thread is open its traffic is read as it arrives
    s.dispatch(msg(chat(5, 'agent-parser', 'agent-parser', 'd')))
    expect(s.getState().threadUnread).toEqual({ 'agent-files': 1 })
    // …and the thread we left starts counting again
    s.dispatch(msg(chat(6, 'root', 'root', 'e')))
    expect(s.getState().threadUnread).toEqual({ 'agent-files': 1, root: 1 })
  })

  it('opens a thread and the chat island from the card, and from an agent selection', () => {
    const s = createStore()
    s.dispatch(msg(chat(1, 'agent-parser', 'agent-parser', 'a')))
    s.setChatOpen(false)
    s.openThread('agent-parser')
    expect(s.getState().activeThread).toBe('agent-parser')
    expect(s.getState().chatOpen).toBe(true)
    expect(s.getState().threadUnread['agent-parser']).toBeUndefined()

    // selecting a card on the canvas focuses that thread and the inspector (B.9/B.8)
    s.dispatch(msg(chat(2, 'agent-files', 'agent-files', 'b')))
    s.selectAgent('agent-files')
    const st = s.getState()
    expect(st.activeThread).toBe('agent-files')
    expect(st.inspectorAgentId).toBe('agent-files')
    expect(st.selectedAgentId).toBe('agent-files')
    expect(st.threadUnread).toEqual({})
  })

  it('clearUnread leaves an already-read thread untouched', () => {
    const unread = { 'agent-x': 2 }
    expect(clearUnread(unread, 'agent-y')).toBe(unread)
    expect(clearUnread(unread, 'agent-x')).toEqual({})
  })
})

// ---------- tab derivation ----------

describe('deriveThreads', () => {
  const agents = {
    'agent-parser': agent('agent-parser', 'working'),
    'agent-files': agent('agent-files', 'done'),
    'agent-old': agent('agent-old', 'working', '2026-09-20T00:00:00.000Z'),
  }

  it('puts root first, then agents with messages or that are live', () => {
    const threads = { 'agent-files': [chat(1, 'agent-files', 'agent-files', 'done here')] }
    const tabs = deriveThreads(threads, { 'agent-files': 1 }, agents)
    expect(tabs.map((t) => t.thread)).toEqual([ROOT_THREAD, 'agent-files', 'agent-parser'])
    expect(tabs[0]).toMatchObject({ label: 'root', count: 0, unread: 0, live: false })
    // agent-files is finished but has history; agent-parser is live with none; agent-old is neither
    expect(tabs[1]).toMatchObject({ label: 'files', count: 1, unread: 1, live: false })
    expect(tabs[2]).toMatchObject({ label: 'parser', count: 0, live: true })
  })

  it('always keeps a tab for the active thread, even an empty one', () => {
    const tabs = deriveThreads({}, {}, {}, 'agent-ghost')
    expect(tabs.map((t) => t.thread)).toEqual([ROOT_THREAD, 'agent-ghost'])
  })

  it('orders the non-root tabs stably', () => {
    const threads = { 'agent-z': [chat(1, 'agent-z', 'agent-z', 'z')], 'agent-a': [chat(2, 'agent-a', 'agent-a', 'a')] }
    expect(deriveThreads(threads, {}, {}).map((t) => t.thread)).toEqual([ROOT_THREAD, 'agent-a', 'agent-z'])
  })
})

// ---------- composer payload and presentation ----------

describe('chatSendPayload', () => {
  it('omits agentId on the root thread and carries reply_to when replying', () => {
    expect(chatSendPayload('hi', ROOT_THREAD)).toEqual({ text: 'hi', thread: 'root' })
    expect(chatSendPayload('hi', 'agent-parser')).toEqual({ text: 'hi', thread: 'agent-parser', agentId: 'agent-parser' })
    expect(chatSendPayload('hi', 'agent-parser', 'chat-12')).toEqual({
      text: 'hi',
      thread: 'agent-parser',
      agentId: 'agent-parser',
      reply_to: 'chat-12',
    })
    expect(chatSendPayload('hi', '')).toEqual({ text: 'hi', thread: 'root' })
  })
})

describe('presentation helpers', () => {
  it('groups consecutive messages by sender', () => {
    const groups = groupMessages([
      chat(1, 'root', 'user', 'a'),
      chat(2, 'root', 'user', 'b'),
      chat(3, 'root', 'root', 'c'),
      chat(4, 'root', 'user', 'd'),
    ])
    expect(groups.map((g) => [g.from, g.mine, g.messages.length])).toEqual([
      ['user', true, 2],
      ['root', false, 1],
      ['user', true, 1],
    ])
  })

  it('colours root in the selection blue and an agent in its status colour', () => {
    const agents = { 'agent-parser': agent('agent-parser', 'blocked') }
    expect(senderColor('root', agents)).toBe('var(--wb-selection)')
    expect(senderColor('agent-parser', agents)).toBe('var(--wb-blocked)')
    expect(senderColor('agent-gone', agents)).toBe('var(--wb-text-2)')
  })

  it('renders a reply stub, falling back to the bare id when the target is not loaded', () => {
    const messages = [chat(1, 'root', 'root', 'the original\n  line')]
    expect(quotedStub(messages, 'chat-1')).toBe('↩ root: the original line')
    expect(quotedStub(messages, 'chat-99')).toBe('↩ chat-99')
    expect(quotedStub(messages, null)).toBeNull()
    expect(quotedStub([chat(1, 'root', 'root', 'x'.repeat(200))], 'chat-1')).toMatch(/…$/)
  })

  it('formats a wall-clock stamp, tolerating junk', () => {
    expect(formatClock('2026-09-20T07:05:00.000Z')).toMatch(/^\d{2}:\d{2}$/)
    expect(formatClock('not a date')).toBe('')
    expect(formatClock(null)).toBe('')
  })
})

// ---------- the state shape the panels read ----------

describe('initial thread state', () => {
  it('is empty and pointed at root', () => {
    const s: StoreState = initialState()
    expect(s.activeThread).toBe(ROOT_THREAD)
    expect(deriveThreads(s.threads, s.threadUnread, s.agents, s.activeThread)).toHaveLength(1)
  })
})
