// Types mirroring docs/CONTRACTS.md §4 (plan model) and §6 (websocket protocol).

export type NodeType = 'hld' | 'lld' | 'er'
export type NodeStatus = 'todo' | 'in_progress' | 'blocked' | 'done'
export type AgentStatus = 'idle' | 'working' | 'blocked' | 'done'

export interface PlanNode {
  id: string
  type: NodeType
  title: string
  status: NodeStatus
  owner: string | null
  depends_on: string[]
  interfaces: Array<string | Record<string, unknown>>
  body?: string
  extra?: Record<string, unknown>
  path?: string | null
}

export interface PlanEdge {
  src: string
  dst: string
  label?: string | null
  diagram: string
}

/** §4 `AgentCard.metrics` — cumulative, every key optional (A.5) */
export interface AgentMetrics {
  elapsed_s?: number
  tool_calls?: number
  /** a deduped list on the server (capped 200); tolerated as a plain count too */
  files_touched?: string[] | number
  tokens_in?: number
  tokens_out?: number
  cost_usd?: number
}

/** §4 `AgentDiagram` — the parsed first mermaid fence of `agents/<id>/diagrams.md` (A.3) */
export interface AgentDiagram {
  mermaid: string
  direction?: string
  nodes?: Array<{ id: string; label?: string }>
  edges?: Array<{ src: string; dst: string; label?: string | null }>
  error?: string | null
}

export interface AgentCard {
  id: string
  assigned_node: string | null
  status: AgentStatus
  claude_agent_ref?: string | null
  ready_deps: string[]
  spawned_at?: string | null
  /** live monitoring (A.5): present only once the agent reports */
  activity?: string | null
  progress?: number | null
  heartbeat_at?: string | null
  finished_at?: string | null
  metrics?: AgentMetrics
  notes?: string
  plan_md?: string
  diagrams_md?: string
  /** derived server-side from `diagrams_md`; never written back */
  diagram?: AgentDiagram | null
}

export interface PlanEvent {
  seq: number
  ts: string
  agent_id: string
  node_id: string | null
  type: string
  note: string
  notified?: string[]
  data?: Record<string, unknown>
}

/**
 * §6 `chat.message` — one per `chat` event, projected by the server's `Bus`. The last 50 per
 * thread are replayed on `client.hello` BEFORE the event replay, so backfill and live traffic
 * overlap on every reconnect: the client dedupes on `id` and never builds threads from
 * `event.append` (B.9 / A.4).
 */
export interface ChatMessage {
  id: string
  /** `'root'` or an agent id */
  thread: string
  from: string
  to?: string | null
  text: string
  ts: string
  seq: number
  reply_to?: string | null
  node_id?: string | null
}

export interface Diagram {
  name: string
  direction: string
  edges: PlanEdge[]
  mermaid: string
  path: string
}

// §1 layout sidecar
export interface LayoutFrame { x: number; y: number; w: number; h: number; collapsed?: boolean }
export interface LayoutNode { x: number; y: number; w?: number; h?: number; parent?: string; pinned?: boolean }
export interface LayoutAgent { x: number; y: number; w?: number; h?: number }
export interface LayoutEdge { startAnchor?: [number, number]; endAnchor?: [number, number]; precise?: boolean }
export interface Layout {
  version?: number
  direction?: string
  updatedAt?: string
  frames?: Record<string, LayoutFrame>
  nodes?: Record<string, LayoutNode>
  agents?: Record<string, LayoutAgent>
  edges?: Record<string, LayoutEdge>
}
export type LayoutPatch = Layout

export interface PlanSnapshot {
  project: string
  rev: number
  nodes: PlanNode[]
  edges: PlanEdge[]
  agents: AgentCard[]
  diagrams: Diagram[]
  layout: Record<string, Layout>
}

// §4 semantic edit ops (canvas → server)
export type EditOp =
  | { op: 'renamed'; id: string; label: string }
  | { op: 'node-created'; id: string; label: string; shape: string; diagram: string }
  | { op: 'deleted'; id: string }
  | { op: 'edge-created'; from: string; to: string; label?: string; diagram: string }
  | { op: 'edge-deleted'; from: string; to: string; diagram: string }
  | { op: 'edge-rerouted'; from: string; to: string; new_from: string; new_to: string; diagram: string }
  | { op: 'status-changed'; id: string; status: NodeStatus }

// §6 envelope
export interface Envelope<T = string, P = unknown> {
  type: T
  payload: P
  seq: number
  ts: string
  replyTo?: number | null
}

export interface NeedsInputPrompt {
  prompt_id: string
  agent_id: string
  node_id: string | null
  question: string
  kind: 'text' | 'choice' | 'confirm'
  choices: string[]
}
export interface DispatchRequest { request_id: string; node_id: string; agent_id: string; job_spec_md: string }
export interface RiskyEditRequest { request_id: string; summary: string; diff: string; affected: string[] }

/** §6 `diagram.request` — one board proposal awaiting the owner's approval (A.2 / B.10) */
export interface DiagramNodeRef { id: string; label?: string | null }
export interface DiagramEdgeRef { src: string; dst: string; label?: string | null }
export interface DiagramRequest {
  request_id: string
  name: string
  mermaid: string
  rationale: string
  nodes_added: DiagramNodeRef[]
  nodes_removed: string[]
  edges_added: DiagramEdgeRef[]
  edges_removed: DiagramEdgeRef[]
  /** set when the `client.hello` re-send recomputes the diff and the proposal no longer validates */
  error?: string | null
}
export interface BridgeStatus { ok: boolean; failures: number; last_error?: string }

export type ServerMessage =
  | Envelope<'plan.snapshot', PlanSnapshot>
  | Envelope<'plan.node.upsert', { node: PlanNode }>
  | Envelope<'plan.node.delete', { id: string }>
  | Envelope<'plan.edge.upsert', { edge: PlanEdge }>
  | Envelope<'plan.edge.delete', { src: string; dst: string; diagram: string }>
  | Envelope<'agent.card.upsert', { agent: AgentCard }>
  | Envelope<'agent.card.delete', { id: string }>
  | Envelope<'event.append', PlanEvent>
  | Envelope<'needs_input', NeedsInputPrompt>
  | Envelope<'dispatch.request', DispatchRequest>
  | Envelope<'risky_edit.request', RiskyEditRequest>
  | Envelope<'diagram.request', DiagramRequest>
  | Envelope<'edit.ack', { forSeq: number; rev: number }>
  | Envelope<'edit.reject', { forSeq: number; reason: string; revert: EditOp[] }>
  | Envelope<'layout.update', { diagram: string; layout: Layout }>
  | Envelope<'server.error', { forSeq?: number; code: string; message: string }>
  | Envelope<'bridge.status', BridgeStatus>
  | Envelope<'chat.message', ChatMessage>

export type ServerMessageType = ServerMessage['type']

export interface ClientPayloads {
  'client.hello': { clientId: string; lastSeq: number; protocol: 1 }
  'canvas.edit': { ops: EditOp[] }
  'canvas.layout': { diagram: string; patch: LayoutPatch }
  /** B.9: `thread` is `'root'` or an agent id; `reply_to` is a `chat.message` id */
  'chat.message': { text: string; agentId?: string; nodeId?: string; thread?: string; reply_to?: string }
  'plan.paste': { text: string }
  'prompt.reply': { prompt_id: string; value: unknown }
  'dispatch.reply': { request_id: string; approved: boolean; note?: string }
  'risky_edit.reply': { request_id: string; approved: boolean; note: string }
  /** B.10: the owner's verdict on a proposed board; `mermaid` (their edit) wins when present */
  'diagram.reply': { request_id: string; approved: boolean; note?: string; mermaid?: string }
  'plan.relayout': { diagram: string }
  'node.status': { id: string; status: NodeStatus }
  /** B.6: the folder's plan textarea, debounced 800ms (A.6 `on_agent_plan_edit`) */
  'agent.plan.edit': { agent_id: string; plan_md: string }
  /** B.6: the folder's mermaid editor; a parse error comes back as `edit.reject` */
  'agent.diagram.edit': { agent_id: string; mermaid: string }
}
export type ClientMessageType = keyof ClientPayloads

export type SocketStatus = 'connecting' | 'open' | 'closed' | 'reconnecting'

export const SERVER_MESSAGE_TYPES: ServerMessageType[] = [
  'plan.snapshot', 'plan.node.upsert', 'plan.node.delete', 'plan.edge.upsert', 'plan.edge.delete',
  'agent.card.upsert', 'agent.card.delete', 'event.append', 'needs_input', 'dispatch.request',
  'risky_edit.request', 'diagram.request', 'edit.ack', 'edit.reject', 'layout.update', 'server.error',
  'bridge.status', 'chat.message',
]

export function edgeKey(src: string, dst: string): string {
  return `${src}__${dst}`
}

/** Primary diagram name: `hld` when present, else the first diagram, else `hld`. */
export function primaryDiagram(snap: Pick<PlanSnapshot, 'diagrams'> | null | undefined): string {
  if (!snap || snap.diagrams.length === 0) return 'hld'
  return snap.diagrams.find((d) => d.name === 'hld')?.name ?? snap.diagrams[0].name
}

/** all depends_on done and status todo */
export function nodeReady(node: PlanNode, byId: Map<string, PlanNode>): boolean {
  if (node.status !== 'todo') return false
  return node.depends_on.every((d) => byId.get(d)?.status === 'done')
}
