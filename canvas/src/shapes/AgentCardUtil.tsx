// Custom `agent-card` shape: one card per subagent, below its owner board. Redesigned for v2
// (plan B.7) into a live monitor:
//
//   ●  agent-parser            working  ⏱ 1m 23s
//   ▸ writing the parser fence tests
//   ▬▬▬▬▬▬▬▬▬░░░░░░  62%
//   14 tools · 6 files · ∑ 41.2k tok · $0.38
//   [ CHAT ] [ OPEN FOLDER ] [ FOCUS NODE ]
//
// The whole card carries the conic running ring while the agent works. Elapsed reads the one
// shared `nowMs` clock in the store (B.1 pattern 7) — never `Date.now()` per shape — and freezes
// at `finished_at`. A heartbeat older than 90s greys the activity line and appends `· stalled?`.
import {
  HTMLContainer,
  Rectangle2d,
  ShapeUtil,
  T,
  resizeBox,
  useEditor,
  type Editor,
  type RecordProps,
  type TLResizeInfo,
  type TLShape,
} from 'tldraw'
import { store, useStore } from '../state/store'
import type { AgentStatus } from '../state/types'
import { agentElapsed, agentStalled, formatCount, formatCost, formatElapsed, formatTokensCompact } from '../state/format'
import { revealAgentFolder } from '../sync/apply'
import { nodeShapeId } from './ids'
import {
  AGENT_CARD_H,
  AGENT_CARD_MIN_H,
  AGENT_CARD_MIN_W,
  AGENT_CARD_W,
  agentCardState,
} from './agentCard'
import './AgentCardUtil.css'

export interface AgentCardProps {
  w: number
  h: number
  agentId: string
  name: string
  status: AgentStatus
  detail: string
  ready: boolean
  nodeId: string
  /** live monitoring (A.5), flattened by `agentCardFacts` */
  activity: string
  /** 0..1, or -1 when the agent has reported no progress */
  progress: number
  spawnedAt: string
  heartbeatAt: string
  finishedAt: string
  toolCalls: number
  filesTouched: number
  tokensIn: number
  tokensOut: number
  costUsd: number
}

declare module 'tldraw' {
  interface TLGlobalShapePropsMap {
    'agent-card': AgentCardProps
  }
}

export type AgentCardShape = TLShape<'agent-card'>

export { AGENT_CARD_H, AGENT_CARD_W, STATUS_COLORS } from './agentCard'

export class AgentCardUtil extends ShapeUtil<AgentCardShape> {
  static override type = 'agent-card' as const
  static override props: RecordProps<AgentCardShape> = {
    w: T.number,
    h: T.number,
    agentId: T.string,
    name: T.string,
    status: T.literalEnum('idle', 'working', 'blocked', 'done'),
    detail: T.string,
    ready: T.boolean,
    nodeId: T.string,
    activity: T.string,
    progress: T.number,
    spawnedAt: T.string,
    heartbeatAt: T.string,
    finishedAt: T.string,
    toolCalls: T.number,
    filesTouched: T.number,
    tokensIn: T.number,
    tokensOut: T.number,
    costUsd: T.number,
  }

  getDefaultProps(): AgentCardProps {
    return {
      w: AGENT_CARD_W,
      h: AGENT_CARD_H,
      agentId: '',
      name: 'agent',
      status: 'idle',
      detail: '',
      ready: false,
      nodeId: '',
      activity: '',
      progress: -1,
      spawnedAt: '',
      heartbeatAt: '',
      finishedAt: '',
      toolCalls: 0,
      filesTouched: 0,
      tokensIn: 0,
      tokensOut: 0,
      costUsd: 0,
    }
  }

  override canEdit() {
    return false
  }
  override canResize() {
    return true
  }
  override canBind() {
    return true
  }
  override hideRotateHandle() {
    return true
  }

  getGeometry(shape: AgentCardShape) {
    return new Rectangle2d({ width: shape.props.w, height: shape.props.h, isFilled: true })
  }

  override onResize(shape: AgentCardShape, info: TLResizeInfo<AgentCardShape>) {
    return resizeBox(shape, info, { minWidth: AGENT_CARD_MIN_W, minHeight: AGENT_CARD_MIN_H })
  }

  component(shape: AgentCardShape) {
    return <AgentCardBody shape={shape} />
  }

  getIndicatorPath(shape: AgentCardShape): Path2D {
    const p = new Path2D()
    p.roundRect(0, 0, shape.props.w, shape.props.h, 10)
    return p
  }
}

const stop = (e: React.SyntheticEvent) => e.stopPropagation()

function AgentCardBody({ shape }: { shape: AgentCardShape }) {
  const editor = useEditor()
  const p = shape.props
  // one interval for the whole canvas, armed only while an agent is live (B.1 pattern 7)
  const nowMs = useStore((s) => s.nowMs)
  const state = agentCardState(p.status)
  const card = { status: p.status, spawned_at: p.spawnedAt, heartbeat_at: p.heartbeatAt, finished_at: p.finishedAt }
  const elapsed = agentElapsed(card, nowMs)
  const stalled = agentStalled(card, nowMs)
  const activity = p.activity || p.detail || (p.nodeId ? `on ${p.nodeId}` : 'unassigned')
  const tokens = p.tokensIn + p.tokensOut
  const a11y = `${p.name} — ${state.label}${p.ready ? ', dependencies done' : ''}${elapsed !== null ? `, ${formatElapsed(elapsed)} elapsed` : ''}${stalled ? ', stalled' : ''}`

  return (
    <HTMLContainer style={{ pointerEvents: 'all', overflow: 'visible' }}>
      <div
        className={`wb-agent ${state.cls}`}
        data-status={p.status}
        style={{ width: p.w, height: p.h }}
        title={a11y}
        aria-label={a11y}
        role="group"
      >
        <div className="wb-agent-row1">
          <span className="wb-agent-dot" title={state.label} aria-label={state.label} role="img" />
          <span className="wb-agent-name">{p.name}</span>
          <span className="wb-agent-status">{state.label}</span>
          {elapsed !== null && (
            <span className="wb-agent-elapsed" title={`elapsed since ${p.spawnedAt}`}>
              ⏱ {formatElapsed(elapsed)}
            </span>
          )}
        </div>

        <div className={`wb-agent-activity${stalled ? ' stalled' : ''}`} title={activity}>
          ▸ {activity}
          {stalled ? ' · stalled?' : ''}
        </div>

        <div className="wb-agent-progress">
          <div
            className="wb-agent-rail"
            role="progressbar"
            aria-valuemin={0}
            aria-valuemax={100}
            aria-valuenow={p.progress >= 0 ? Math.round(p.progress * 100) : undefined}
          >
            <span className="wb-agent-rail-fill" style={{ width: `${Math.max(0, p.progress) * 100}%` }} />
          </div>
          {p.progress >= 0 && <span className="wb-agent-pct">{Math.round(p.progress * 100)}%</span>}
        </div>

        <div className="wb-agent-metrics" title={`${formatCount(p.toolCalls)} tool calls · ${formatCount(p.filesTouched)} files touched · ∑ ${formatTokensCompact(tokens)} tokens · ${formatCost(p.costUsd)}`}>
          {formatCount(p.toolCalls)} tools · {formatCount(p.filesTouched)} files · ∑ {formatTokensCompact(tokens)} tok ·{' '}
          {formatCost(p.costUsd)}
        </div>

        <div className="wb-agent-actions">
          <button
            type="button"
            className="wb-agent-btn"
            title={`Open the ${p.agentId} chat thread`}
            onPointerDown={stop}
            onClick={(e) => {
              e.stopPropagation()
              // B.9: the card's Chat button focuses this agent's THREAD (and expands the island),
              // it no longer just "selects" the agent for a one-way textarea
              store.openThread(p.agentId)
              store.inspectAgent(p.agentId)
            }}
          >
            Chat
          </button>
          <button
            type="button"
            className="wb-agent-btn"
            onPointerDown={stop}
            onClick={(e) => {
              e.stopPropagation()
              revealAgentFolder(editor, p.agentId)
            }}
          >
            Open folder
          </button>
          <button
            type="button"
            className="wb-agent-btn"
            disabled={!p.nodeId}
            onPointerDown={stop}
            onClick={(e) => {
              e.stopPropagation()
              focusNode(editor, p.nodeId)
            }}
          >
            Focus node
          </button>
        </div>
      </div>
    </HTMLContainer>
  )
}

function focusNode(editor: Editor, nodeId: string): void {
  if (!nodeId) return
  const id = nodeShapeId(nodeId)
  if (!editor.getShape(id)) return
  editor.select(id)
  editor.zoomToSelection({ animation: { duration: 250 } })
}
