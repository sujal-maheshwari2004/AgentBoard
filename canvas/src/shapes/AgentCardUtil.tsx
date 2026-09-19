// Custom `agent-card` shape: one card per subagent, outside the plan frame.
import {
  HTMLContainer,
  Rectangle2d,
  ShapeUtil,
  T,
  resizeBox,
  type RecordProps,
  type TLResizeInfo,
  type TLShape,
} from 'tldraw'
import { store } from '../state/store'
import type { AgentStatus } from '../state/types'
import { nodeShapeId } from './ids'

export interface AgentCardProps {
  w: number
  h: number
  agentId: string
  name: string
  status: AgentStatus
  detail: string
  ready: boolean
  nodeId: string
}

declare module 'tldraw' {
  interface TLGlobalShapePropsMap {
    'agent-card': AgentCardProps
  }
}

export type AgentCardShape = TLShape<'agent-card'>

export const AGENT_CARD_W = 240
export const AGENT_CARD_H = 120

export const STATUS_COLORS: Record<AgentStatus, string> = {
  idle: '#9aa0a6',
  working: '#1a73e8',
  blocked: '#d93025',
  done: '#188038',
}

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
  }

  getDefaultProps(): AgentCardProps {
    return { w: AGENT_CARD_W, h: AGENT_CARD_H, agentId: '', name: 'agent', status: 'idle', detail: '', ready: false, nodeId: '' }
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
    return resizeBox(shape, info, { minWidth: 160, minHeight: 80 })
  }

  component(shape: AgentCardShape) {
    const { agentId, name, status, detail, ready, nodeId, w, h } = shape.props
    const editor = this.editor
    const color = STATUS_COLORS[status] ?? STATUS_COLORS.idle
    const stop = (e: React.SyntheticEvent) => e.stopPropagation()
    const onChat = () => store.selectAgent(agentId)
    const onFocus = () => {
      if (!nodeId) return
      const id = nodeShapeId(nodeId)
      if (!editor.getShape(id)) return
      editor.select(id)
      editor.zoomToSelection({ animation: { duration: 250 } })
    }
    return (
      <HTMLContainer
        style={{
          pointerEvents: 'all',
          width: w,
          height: h,
          boxSizing: 'border-box',
          background: '#fff',
          border: `2px solid ${color}`,
          borderRadius: 8,
          padding: '8px 10px',
          fontFamily: 'system-ui, sans-serif',
          fontSize: 12,
          color: '#202124',
          display: 'flex',
          flexDirection: 'column',
          gap: 4,
          overflow: 'hidden',
        }}
      >
        <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
          <span
            title={status}
            style={{ width: 10, height: 10, borderRadius: 5, background: color, display: 'inline-block', flex: 'none' }}
          />
          <strong style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', flex: 1 }}>{name}</strong>
          <span style={{ color, fontSize: 11 }}>{status}</span>
          {ready && (
            <span
              title="all dependencies done"
              style={{ background: '#188038', color: '#fff', borderRadius: 4, padding: '0 5px', fontSize: 10 }}
            >
              READY
            </span>
          )}
        </div>
        <div style={{ color: '#5f6368', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
          {nodeId ? `on ${nodeId}` : 'unassigned'}
        </div>
        {detail && (
          <div style={{ color: '#3c4043', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{detail}</div>
        )}
        <div style={{ marginTop: 'auto', display: 'flex', gap: 6 }}>
          <button onPointerDown={stop} onClick={onChat} style={btn}>
            Chat
          </button>
          <button onPointerDown={stop} onClick={onFocus} disabled={!nodeId} style={btn}>
            Focus node
          </button>
        </div>
      </HTMLContainer>
    )
  }

  getIndicatorPath(shape: AgentCardShape): Path2D {
    const p = new Path2D()
    p.rect(0, 0, shape.props.w, shape.props.h)
    return p
  }
}

const btn: React.CSSProperties = {
  font: 'inherit',
  fontSize: 11,
  padding: '2px 8px',
  border: '1px solid #dadce0',
  borderRadius: 4,
  background: '#f8f9fa',
  cursor: 'pointer',
}
