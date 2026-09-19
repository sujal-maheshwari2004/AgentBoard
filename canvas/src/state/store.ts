// Small plain store (no deps) exposed to React via useSyncExternalStore.
// Holds the last-known server truth plus UI-only state. Files are truth: nothing here persists.
import { useSyncExternalStore } from 'react'
import type {
  AgentCard, BridgeStatus, Diagram, DispatchRequest, Layout, NeedsInputPrompt, PlanEdge, PlanEvent,
  PlanNode, RiskyEditRequest, ServerMessage, SocketStatus,
} from './types'
import { edgeKey, primaryDiagram } from './types'

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
  diagram: string
  hasSnapshot: boolean
  prompts: NeedsInputPrompt[]
  dispatches: DispatchRequest[]
  riskyEdits: RiskyEditRequest[]
  ticker: TickerEntry[]
  bridge: BridgeStatus | null
  socket: SocketStatus
  selectedAgentId: string | null
  lastSeq: number
  lastAck: { forSeq: number; rev: number } | null
  lastReject: { forSeq: number; reason: string; seq: number } | null
  lastError: { code: string; message: string; seq: number } | null
}

export const TICKER_LIMIT = 200

export function initialState(): StoreState {
  return {
    project: '',
    rev: 0,
    nodes: {},
    edges: {},
    agents: {},
    diagrams: [],
    layout: {},
    diagram: 'hld',
    hasSnapshot: false,
    prompts: [],
    dispatches: [],
    riskyEdits: [],
    ticker: [],
    bridge: null,
    socket: 'connecting',
    selectedAgentId: null,
    lastSeq: 0,
    lastAck: null,
    lastReject: null,
    lastError: null,
  }
}

function pushTicker(ticker: TickerEntry[], entry: TickerEntry): TickerEntry[] {
  const next = [...ticker, entry]
  return next.length > TICKER_LIMIT ? next.slice(next.length - TICKER_LIMIT) : next
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
        diagram: primaryDiagram(snap),
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
      return { ...state, agents, selectedAgentId }
    }
    case 'event.append': {
      const ev = msg.payload
      return {
        ...state,
        lastSeq: Math.max(state.lastSeq, ev.seq),
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
        lastError: { code: msg.payload.code, message: msg.payload.message, seq: msg.seq },
        ticker: pushTicker(state.ticker, { seq: msg.seq, ts: msg.ts, kind: 'error', text: `server error ${msg.payload.code}: ${msg.payload.message}` }),
      }
    case 'bridge.status':
      return { ...state, bridge: msg.payload }
    default:
      return state
  }
}

export interface Store {
  getState(): StoreState
  subscribe(listener: () => void): () => void
  dispatch(msg: ServerMessage): void
  set(patch: Partial<StoreState> | ((s: StoreState) => Partial<StoreState>)): void
  selectAgent(id: string | null): void
  setSocketStatus(status: SocketStatus): void
  removePrompt(promptId: string): void
  removeDispatch(requestId: string): void
  removeRiskyEdit(requestId: string): void
  note(kind: string, text: string): void
}

export function createStore(init: StoreState = initialState()): Store {
  let state = init
  const listeners = new Set<() => void>()
  const emit = () => listeners.forEach((l) => l())
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
    },
    set(patch) {
      const p = typeof patch === 'function' ? patch(state) : patch
      state = { ...state, ...p }
      emit()
    },
    selectAgent(id) {
      store.set({ selectedAgentId: id })
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
