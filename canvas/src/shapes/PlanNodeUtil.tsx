// Custom `plan-node` shape (plan B.3). `geo` cannot do what B.1 requires — text below the box, a
// conic-gradient ring, a dotted not-yet-dispatched outline, a type stripe, zoom-adaptive opacity —
// so the shape type changes while the id grammar (`n_<nodeId>`) and `meta` stay exactly as they were.
import {
  HTMLContainer,
  Rectangle2d,
  ShapeUtil,
  T,
  resizeBox,
  useValue,
  type RecordProps,
  type TLResizeInfo,
  type TLShape,
} from 'tldraw'
import type { NodeStatus, NodeType } from '../state/types'
import { DEGRADE_ZOOM, zoomAtom } from '../sync/zoom'
import './PlanNodeUtil.css'

export interface PlanNodeProps {
  w: number
  h: number
  nodeId: string
  title: string
  subtitle: string
  type: NodeType
  status: NodeStatus
  ready: boolean
  dispatched: boolean
  owner: string
}

declare module 'tldraw' {
  interface TLGlobalShapePropsMap {
    'plan-node': PlanNodeProps
  }
}

export type PlanNodeShape = TLShape<'plan-node'>

export const PLAN_NODE_W = 200
export const PLAN_NODE_H = 64
export const PLAN_NODE_MIN_W = 140
export const PLAN_NODE_MIN_H = 48

/** class + a11y text for one (status, ready, dispatched) triple — B.1 item 12: colour is never the only signal */
export function planNodeState(props: Pick<PlanNodeProps, 'status' | 'ready' | 'dispatched'>): {
  cls: string
  label: string
} {
  switch (props.status) {
    case 'in_progress':
      return { cls: 'running', label: 'running' }
    case 'blocked':
      return { cls: 'waiting', label: 'blocked' }
    case 'done':
      return { cls: 'done', label: 'done' }
    default:
      if (props.ready) return { cls: 'ready', label: 'ready to dispatch' }
      if (!props.dispatched) return { cls: 'undispatched', label: 'not dispatched' }
      return { cls: 'idle', label: 'todo' }
  }
}

export class PlanNodeUtil extends ShapeUtil<PlanNodeShape> {
  static override type = 'plan-node' as const
  static override props: RecordProps<PlanNodeShape> = {
    w: T.number,
    h: T.number,
    nodeId: T.string,
    title: T.string,
    subtitle: T.string,
    type: T.literalEnum('hld', 'lld', 'er'),
    status: T.literalEnum('todo', 'in_progress', 'blocked', 'done'),
    ready: T.boolean,
    dispatched: T.boolean,
    owner: T.string,
  }

  getDefaultProps(): PlanNodeProps {
    return {
      w: PLAN_NODE_W,
      h: PLAN_NODE_H,
      nodeId: '',
      title: 'node',
      subtitle: '',
      type: 'hld',
      status: 'todo',
      ready: false,
      dispatched: false,
      owner: '',
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

  getGeometry(shape: PlanNodeShape) {
    return new Rectangle2d({ width: shape.props.w, height: shape.props.h, isFilled: true })
  }

  override onResize(shape: PlanNodeShape, info: TLResizeInfo<PlanNodeShape>) {
    return resizeBox(shape, info, { minWidth: PLAN_NODE_MIN_W, minHeight: PLAN_NODE_MIN_H })
  }

  component(shape: PlanNodeShape) {
    return <PlanNodeBody shape={shape} />
  }

  getIndicatorPath(shape: PlanNodeShape): Path2D {
    const { w, h } = shape.props
    const p = new Path2D()
    p.roundRect(0, 0, w, h, 8)
    return p
  }
}

function PlanNodeBody({ shape }: { shape: PlanNodeShape }) {
  const { w, h, nodeId, title, subtitle, type, status, ready, dispatched } = shape.props
  const zoom = useValue('wb-zoom', () => zoomAtom.get(), [])
  const state = planNodeState({ status, ready, dispatched })
  const a11y = `${title || nodeId} — ${type.toUpperCase()}, ${state.label}`

  if (zoom < DEGRADE_ZOOM) {
    // Dagster: below a few pixels a box degrades to a dot and the label drops.
    return (
      <HTMLContainer style={{ pointerEvents: 'all', overflow: 'visible' }}>
        <div style={{ position: 'relative', width: w, height: h }}>
          <span
            className="wb-node-dot"
            data-status={status}
            data-ready={ready ? 'true' : 'false'}
            title={a11y}
            aria-label={a11y}
            role="img"
          />
        </div>
      </HTMLContainer>
    )
  }

  return (
    <HTMLContainer style={{ pointerEvents: 'all', overflow: 'visible' }}>
      <div
        className={`wb-node ${state.cls}`}
        data-type={type}
        data-status={status}
        data-ready={ready ? 'true' : 'false'}
        data-dispatched={dispatched ? 'true' : 'false'}
        style={{ width: w, height: h }}
        title={a11y}
        aria-label={a11y}
        role="group"
      >
        <span className="wb-node-stripe" aria-hidden="true" />
        <span className="wb-node-type">{type}</span>
        {ready && <span className="wb-badge">Ready</span>}
        {status === 'done' && (
          <span className="wb-node-check" aria-hidden="true">
            ✓
          </span>
        )}
        <span className="wb-node-status" title={state.label} aria-label={state.label} role="img" />
        <div className="wb-node-label">
          <div className="wb-node-title">{title || nodeId}</div>
          {subtitle && <div className="wb-node-sub">{subtitle}</div>}
        </div>
      </div>
    </HTMLContainer>
  )
}
