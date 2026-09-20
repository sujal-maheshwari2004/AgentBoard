// Small plain store (no deps) exposed to React via useSyncExternalStore.
// Holds the last-known server truth plus UI-only state. Files are truth: nothing here persists.
import { useSyncExternalStore } from 'react'
import type {
  AgentCard, BridgeStatus, ChatMessage, Diagram, DiagramRequest, DispatchRequest, Layout, NeedsInputPrompt, PlanEdge,
  PlanEvent, PlanNode, RiskyEditRequest, ServerMessage, SocketStatus,
} from './types'
import { edgeKey, primaryDiagram } from './types'
import { anyAgentLive, isAgentLive } from './format'
import { ROOT_THREAD, bumpUnread, clearUnread, mergeChatMessage, threadOf } from './threads'
import { DEFAULT_BOARD, isBoardName } from '../sync/boards'

export { anyAgentLive, isAgentLive }

export interface TickerEntry {
  seq: number
  ts: string
  kind: string
  text: string
}

export interface StoreState {
  project: string
  rev: number
  nodes: Record<string, PlanNode>
  edges: Record<string, PlanEdge>
  agents: Record<string, AgentCard>
  diagrams: Diagram[]
  layout: Record<string, Layout>
  /**
   * The board the camera is on (B.5/B.11): it keys `canvas.layout`, `plan.relayout` and the
   * `layout.update` guard, and the segmented switcher both sets it and follows it.
   */
  activeBoard: string
  hasSnapshot: boolean
  prompts: NeedsInputPrompt[]
  dispatches: DispatchRequest[]
  riskyEdits: RiskyEditRequest[]
  /** B.10: board proposals awaiting the owner; re-sent on `client.hello`, so dedupe by `request_id` */
  diagramRequests: DiagramRequest[]
  ticker: TickerEntry[]
  /**
   * The retained raw events (B.11), capped at `EVENT_LIMIT`. The flattened `ticker` stays as it
   * is; feeds (the agent inspector, the reworked ticker) need `data`/`agent_id`/`node_id`.
   */
  events: PlanEvent[]
  /** `chat.message`s by thread, each list in `seq` order and deduped by `id` (B.9) */
  threads: Record<string, ChatMessage[]>
  threadUnread: Record<string, number>
  /** `'root'` or an agent id; the thread the composer sends to */
  activeThread: string
  /** the chat island is expanded (false = the 44px unread pill) */
  chatOpen: boolean
  /** the agent the inspector shows; `selectedAgentId` is the canvas selection that seeds it */
  inspectorAgentId: string | null
  bridge: BridgeStatus | null
  socket: SocketStatus
  selectedAgentId: string | null
  /**
   * The one shared clock every elapsed readout reads (B.1 pattern 7 / B.11): a single 1s
   * interval, armed only while some agent is live and cleared the moment none is. Shapes never
   * call `Date.now()` themselves.
   */
  nowMs: number
  lastSeq: number
  lastAck: { forSeq: number; rev: number } | null
  lastReject: { forSeq: number; reason: string; seq: number } | null
  lastError: { code: string; message: string; seq: number; forSeq?: number } | null
}

export const TICKER_LIMIT = 200
/** raw events retained for the feeds (B.11) */
export const EVENT_LIMIT = 500

export function initialState(): StoreState {
  return {
    project: '',
    rev: 0,
    nodes: {},
    edges: {},
    agents: {},
    diagrams: [],
    layout: {},
    activeBoard: DEFAULT_BOARD,
    hasSnapshot: false,
    prompts: [],
    dispatches: [],
    riskyEdits: [],
    diagramRequests: [],
    ticker: [],
    events: [],
    threads: {},
    threadUnread: {},
    activeThread: ROOT_THREAD,
    chatOpen: true,
    inspectorAgentId: null,
    bridge: null,
    socket: 'connecting',
    selectedAgentId: null,
    nowMs: Date.now(),
    lastSeq: 0,
    lastAck: null,
    lastReject: null,
    lastError: null,
  }
}

/** the board a fresh snapshot lands on: `hld` when present, else the first board diagram */
function initialBoard(snap: Parameters<typeof primaryDiagram>[0]): string {
  const name = primaryDiagram(snap)
  if (isBoardName(name)) return name
  for (const d of snap?.diagrams ?? []) if (isBoardName(d.name)) return d.name
  return DEFAULT_BOARD
}

function pushTicker(ticker: TickerEntry[], entry: TickerEntry): TickerEntry[] {
  const next = [...ticker, entry]
  return next.length > TICKER_LIMIT ? next.slice(next.length - TICKER_LIMIT) : next
}

function pushEvent(events: PlanEvent[], ev: PlanEvent): PlanEvent[] {
  const next = [...events, ev]
  return next.length > EVENT_LIMIT ? next.slice(next.length - EVENT_LIMIT) : next
}

function describeEvent(ev: PlanEvent): string {
  const who = ev.agent_id
  const where = ev.node_id ? ` on ${ev.node_id}` : ''
  const note = ev.note ? `: ${ev.note}` : ''
  return `${who} ${ev.type}${where}${note}`
}

/** Pure reducer for every server → client message type (CONTRACTS §6). */
export function reduce(state: StoreState, msg: ServerMessage): StoreState {
  switch (msg.type) {
    case 'plan.snapshot': {
      const snap = msg.payload
      const nodes: Record<string, PlanNode> = {}
      for (const n of snap.nodes) nodes[n.id] = n
      const edges: Record<string, PlanEdge> = {}
      for (const e of snap.edges) edges[edgeKey(e.src, e.dst)] = e
      const agents: Record<string, AgentCard> = {}
      for (const a of snap.agents) agents[a.id] = a
      return {
        ...state,
        project: snap.project,
        rev: snap.rev,
        nodes,
        edges,
        agents,
        diagrams: snap.diagrams,
        layout: snap.layout ?? {},
        activeBoard: initialBoard(snap),
        hasSnapshot: true,
        ticker: pushTicker(state.ticker, { seq: msg.seq, ts: msg.ts, kind: 'snapshot', text: `snapshot rev ${snap.rev} (${snap.nodes.length} nodes, ${snap.agents.length} agents)` }),
      }
    }
    case 'plan.node.upsert': {
      const node = msg.payload.node
      return { ...state, nodes: { ...state.nodes, [node.id]: node } }
    }
    case 'plan.node.delete': {
      const nodes = { ...state.nodes }
      delete nodes[msg.payload.id]
      const edges: Record<string, PlanEdge> = {}
      for (const [k, e] of Object.entries(state.edges)) {
        if (e.src !== msg.payload.id && e.dst !== msg.payload.id) edges[k] = e
      }
      return { ...state, nodes, edges }
    }
    case 'plan.edge.upsert': {
      const e = msg.payload.edge
      return { ...state, edges: { ...state.edges, [edgeKey(e.src, e.dst)]: e } }
    }
    case 'plan.edge.delete': {
      const edges = { ...state.edges }
      delete edges[edgeKey(msg.payload.src, msg.payload.dst)]
      return { ...state, edges }
    }
    case 'agent.card.upsert': {
      const a = msg.payload.agent
      return { ...state, agents: { ...state.agents, [a.id]: a } }
    }
    case 'agent.card.delete': {
      const agents = { ...state.agents }
      delete agents[msg.payload.id]
      const selectedAgentId = state.selectedAgentId === msg.payload.id ? null : state.selectedAgentId
      const inspectorAgentId = state.inspectorAgentId === msg.payload.id ? null : state.inspectorAgentId
      return { ...state, agents, selectedAgentId, inspectorAgentId }
    }
    case 'event.append': {
      const ev = msg.payload
      return {
        ...state,
        lastSeq: Math.max(state.lastSeq, ev.seq),
        events: pushEvent(state.events, ev),
        ticker: pushTicker(state.ticker, { seq: ev.seq, ts: ev.ts, kind: ev.type, text: describeEvent(ev) }),
      }
    }
    case 'needs_input': {
      const p = msg.payload
      if (state.prompts.some((q) => q.prompt_id === p.prompt_id)) return state
      return {
        ...state,
        prompts: [...state.prompts, p],
        ticker: pushTicker(state.ticker, { seq: msg.seq, ts: msg.ts, kind: 'needs_input', text: `${p.agent_id} asks: ${p.question}` }),
      }
    }
    case 'dispatch.request': {
      const d = msg.payload
      if (state.dispatches.some((q) => q.request_id === d.request_id)) return state
      return {
        ...state,
        dispatches: [...state.dispatches, d],
        ticker: pushTicker(state.ticker, { seq: msg.seq, ts: msg.ts, kind: 'dispatch', text: `dispatch proposed: ${d.node_id} → ${d.agent_id}` }),
      }
    }
    case 'risky_edit.request': {
      const r = msg.payload
      if (state.riskyEdits.some((q) => q.request_id === r.request_id)) return state
      return {
        ...state,
        riskyEdits: [...state.riskyEdits, r],
        ticker: pushTicker(state.ticker, { seq: msg.seq, ts: msg.ts, kind: 'risky_edit', text: `risky edit: ${r.summary}` }),
      }
    }
    case 'diagram.request': {
      const d = msg.payload
      // A re-send (hello replay) REPLACES the entry: its diff was recomputed and it may now
      // carry an `error`, which the modal has to show.
      const at = state.diagramRequests.findIndex((q) => q.request_id === d.request_id)
      return {
        ...state,
        diagramRequests: at >= 0 ? state.diagramRequests.map((q, i) => (i === at ? d : q)) : [...state.diagramRequests, d],
        ticker: pushTicker(state.ticker, { seq: msg.seq, ts: msg.ts, kind: 'diagram', text: `diagram proposed: ${d.name} (req ${d.request_id})` }),
      }
    }
    case 'edit.ack':
      return { ...state, rev: msg.payload.rev, lastAck: msg.payload }
    case 'edit.reject':
      return {
        ...state,
        lastReject: { forSeq: msg.payload.forSeq, reason: msg.payload.reason, seq: msg.seq },
        ticker: pushTicker(state.ticker, { seq: msg.seq, ts: msg.ts, kind: 'reject', text: `edit rejected: ${msg.payload.reason}` }),
      }
    case 'layout.update':
      return { ...state, layout: { ...state.layout, [msg.payload.diagram]: msg.payload.layout } }
    case 'server.error':
      return {
        ...state,
        lastError: { code: msg.payload.code, message: msg.payload.message, seq: msg.seq, forSeq: msg.payload.forSeq },
        ticker: pushTicker(state.ticker, { seq: msg.seq, ts: msg.ts, kind: 'error', text: `server error ${msg.payload.code}: ${msg.payload.message}` }),
      }
    case 'bridge.status':
      return { ...state, bridge: msg.payload }
    case 'chat.message': {
      // backfill (50 per thread, replayed before the events on `client.hello`) and live traffic
      // overlap on every reconnect, so `id` decides and `seq` orders (B.9 / A.4)
      const thread = threadOf(msg.payload)
      const merged = mergeChatMessage(state.threads[thread], msg.payload)
      if (merged === null) return state
      const threads = { ...state.threads, [thread]: merged }
      if (thread === state.activeThread) return { ...state, threads }
      return { ...state, threads, threadUnread: bumpUnread(state.threadUnread, thread) }
    }
    default:
      return state
  }
}

export interface Store {
  getState(): StoreState
  subscribe(listener: () => void): () => void
  dispatch(msg: ServerMessage): void
  set(patch: Partial<StoreState> | ((s: StoreState) => Partial<StoreState>)): void
  /** the canvas selection: also seeds the inspector and the active chat thread (B.9) */
  selectAgent(id: string | null): void
  /** the inspector alone (its close button), leaving the canvas selection untouched */
  inspectAgent(id: string | null): void
  /** switch threads; opening a thread clears its unread count (B.9) */
  setActiveThread(thread: string): void
  /** the agent card's Chat button: focus the thread and expand the chat island */
  openThread(thread: string): void
  setChatOpen(open: boolean): void
  setActiveBoard(board: string): void
  setSocketStatus(status: SocketStatus): void
  removePrompt(promptId: string): void
  removeDispatch(requestId: string): void
  removeRiskyEdit(requestId: string): void
  removeDiagramRequest(requestId: string): void
  note(kind: string, text: string): void
  /** stop the `nowMs` interval (unmount / tests); it re-arms on the next live agent */
  stopClock(): void
}

export interface StoreTimers {
  setInterval(fn: () => void, ms: number): unknown
  clearInterval(handle: unknown): void
  now(): number
}

const defaultTimers: StoreTimers = {
  setInterval: (fn, ms) => {
    const h = setInterval(fn, ms)
    // node (tests, SSR) must not be held open by the canvas clock; browsers return a number
    ;(h as unknown as { unref?: () => void }).unref?.()
    return h
  },
  clearInterval: (h) => clearInterval(h as ReturnType<typeof setInterval>),
  now: () => Date.now(),
}

export const CLOCK_INTERVAL_MS = 1000

export function createStore(init: StoreState = initialState(), timers: StoreTimers = defaultTimers): Store {
  let state = init
  const listeners = new Set<() => void>()
  const emit = () => listeners.forEach((l) => l())
  let clock: unknown = null
  /**
   * Bind the ticker to liveness (Temporal/Prefect): arm one interval when an agent is live,
   * tear it down the moment none is. Called after every state change that can touch `agents`.
   */
  const syncClock = () => {
    const live = anyAgentLive(state.agents)
    if (live && clock === null) {
      // no emit here: arming is invisible, the first tick 1s later is what re-renders
      state = { ...state, nowMs: timers.now() }
      clock = timers.setInterval(() => store.set({ nowMs: timers.now() }), CLOCK_INTERVAL_MS)
    } else if (!live && clock !== null) {
      timers.clearInterval(clock)
      clock = null
    }
  }
  const store: Store = {
    getState: () => state,
    subscribe(listener) {
      listeners.add(listener)
      return () => listeners.delete(listener)
    },
    dispatch(msg) {
      const next = reduce(state, msg)
      if (next !== state) {
        state = next
        emit()
      }
      // only these three can change whether anything is live (B.11)
      if (msg.type === 'agent.card.upsert' || msg.type === 'agent.card.delete' || msg.type === 'plan.snapshot') syncClock()
    },
    set(patch) {
      const p = typeof patch === 'function' ? patch(state) : patch
      state = { ...state, ...p }
      emit()
    },
    selectAgent(id) {
      if (id === null) {
        store.set({ selectedAgentId: null, inspectorAgentId: null })
        return
      }
      store.set((s) => ({
        selectedAgentId: id,
        inspectorAgentId: id,
        activeThread: id,
        threadUnread: clearUnread(s.threadUnread, id),
      }))
    },
    inspectAgent(id) {
      if (state.inspectorAgentId !== id) store.set({ inspectorAgentId: id })
    },
    setActiveThread(thread) {
      const name = thread || ROOT_THREAD
      store.set((s) => ({ activeThread: name, threadUnread: clearUnread(s.threadUnread, name) }))
    },
    openThread(thread) {
      store.setActiveThread(thread)
      store.set({ chatOpen: true })
    },
    setChatOpen(open) {
      if (state.chatOpen !== open) store.set({ chatOpen: open })
    },
    setActiveBoard(board) {
      if (state.activeBoard !== board) store.set({ activeBoard: board })
    },
    setSocketStatus(status) {
      if (state.socket !== status) store.set({ socket: status })
    },
    removePrompt(promptId) {
      store.set((s) => ({ prompts: s.prompts.filter((p) => p.prompt_id !== promptId) }))
    },
    removeDispatch(requestId) {
      store.set((s) => ({ dispatches: s.dispatches.filter((d) => d.request_id !== requestId) }))
    },
    removeRiskyEdit(requestId) {
      store.set((s) => ({ riskyEdits: s.riskyEdits.filter((r) => r.request_id !== requestId) }))
    },
    removeDiagramRequest(requestId) {
      store.set((s) => ({ diagramRequests: s.diagramRequests.filter((d) => d.request_id !== requestId) }))
    },
    stopClock() {
      if (clock !== null) {
        timers.clearInterval(clock)
        clock = null
      }
    },
    note(kind, text) {
      store.set((s) => ({
        ticker: pushTicker(s.ticker, { seq: -1, ts: new Date().toISOString(), kind, text }),
      }))
    },
  }
  return store
}

export const store: Store = createStore()

export function useStore<T>(selector: (s: StoreState) => T): T {
  return useSyncExternalStore(store.subscribe, () => selector(store.getState()), () => selector(store.getState()))
}
