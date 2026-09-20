// Custom `agent-folder` shape (plan B.6): one opt-in container per subagent holding the two
// files the canvas can edit mid-run — `agents/<id>/plan.md` and `agents/<id>/diagrams.md`.
//
// Rendering follows the ComfyUI group trick (B.0): the title strip is filled at 0.25 alpha and
// the body is filled at 0.25 alpha over it, so the header comes out darker for free; the border
// is the same hue at full alpha and the title text is the folder's own colour. The hue is the
// agent's status colour, so a working folder reads amber at a glance.
//
// Interaction: `pointerEvents: 'all'` on the container and `stopPropagation` on the pointer-down
// of every control, or tldraw eats the interaction before the DOM ever sees it.
import { useCallback, useEffect, useRef, useState } from 'react'
import {
  HTMLContainer,
  Rectangle2d,
  ShapeUtil,
  T,
  resizeBox,
  useEditor,
  type RecordProps,
  type TLResizeInfo,
  type TLShape,
} from 'tldraw'
import { useStore } from '../state/store'
import type { AgentStatus } from '../state/types'
import { getWiring } from '../sync/wire'
import { toggleFolder } from '../sync/frame'
import {
  EDIT_DEBOUNCE_MS,
  FOLDER_COLLAPSED_H,
  FOLDER_H,
  FOLDER_MIN_H,
  FOLDER_MIN_W,
  FOLDER_W,
  folderHue,
} from './agentFolder'
import './AgentFolderUtil.css'

export interface AgentFolderProps {
  w: number
  h: number
  agentId: string
  ownerNode: string
  status: AgentStatus
  planMd: string
  mermaid: string
  /** the server's own parse error for `diagrams.md`, shown even before we edit anything */
  diagramError: string
}

declare module 'tldraw' {
  interface TLGlobalShapePropsMap {
    'agent-folder': AgentFolderProps
  }
}

export type AgentFolderShape = TLShape<'agent-folder'>

export { FOLDER_COLLAPSED_H, FOLDER_H, FOLDER_W }

export class AgentFolderUtil extends ShapeUtil<AgentFolderShape> {
  static override type = 'agent-folder' as const
  static override props: RecordProps<AgentFolderShape> = {
    w: T.number,
    h: T.number,
    agentId: T.string,
    ownerNode: T.string,
    status: T.literalEnum('idle', 'working', 'blocked', 'done'),
    planMd: T.string,
    mermaid: T.string,
    diagramError: T.string,
  }

  getDefaultProps(): AgentFolderProps {
    return {
      w: FOLDER_W,
      h: FOLDER_H,
      agentId: '',
      ownerNode: '',
      status: 'idle',
      planMd: '',
      mermaid: '',
      diagramError: '',
    }
  }

  override canEdit() {
    return false
  }
  override canResize() {
    return true
  }
  override canBind() {
    return false
  }
  override hideRotateHandle() {
    return true
  }

  getGeometry(shape: AgentFolderShape) {
    return new Rectangle2d({ width: shape.props.w, height: shape.props.h, isFilled: true })
  }

  override onResize(shape: AgentFolderShape, info: TLResizeInfo<AgentFolderShape>) {
    return resizeBox(shape, info, { minWidth: FOLDER_MIN_W, minHeight: FOLDER_MIN_H })
  }

  component(shape: AgentFolderShape) {
    return <AgentFolderBody shape={shape} />
  }

  getIndicatorPath(shape: AgentFolderShape): Path2D {
    const p = new Path2D()
    p.roundRect(0, 0, shape.props.w, shape.props.h, 10)
    return p
  }
}

const stop = (e: React.SyntheticEvent) => e.stopPropagation()

function AgentFolderBody({ shape }: { shape: AgentFolderShape }) {
  const editor = useEditor()
  const { w, h, agentId, ownerNode, status, planMd, mermaid, diagramError } = shape.props
  const collapsed = !!shape.meta.collapsed
  const hue = folderHue(status)
  const a11y = `agent folder ${agentId} — ${status}${ownerNode ? `, on ${ownerNode}` : ''}`

  return (
    <HTMLContainer style={{ pointerEvents: 'all', overflow: 'visible' }}>
      <div
        className={`wb-folder${collapsed ? ' collapsed' : ''}`}
        data-status={status}
        style={{ width: w, height: collapsed ? FOLDER_COLLAPSED_H : h, ['--wb-folder-hue' as string]: hue }}
        title={a11y}
        aria-label={a11y}
        role="group"
      >
        <div className="wb-folder-head">
          <button
            type="button"
            className="wb-folder-chevron"
            title={collapsed ? `Expand ${agentId}` : `Collapse ${agentId}`}
            aria-label={collapsed ? `Expand ${agentId}` : `Collapse ${agentId}`}
            aria-expanded={!collapsed}
            onPointerDown={stop}
            onClick={(e) => {
              e.stopPropagation()
              toggleFolder(editor, shape.id)
            }}
          >
            {collapsed ? '▸' : '▾'}
          </button>
          <span className="wb-folder-title">{agentId}</span>
          <span className="wb-folder-pill" title={status} aria-label={status}>
            {status}
          </span>
          {ownerNode && <span className="wb-folder-node">on {ownerNode}</span>}
        </div>
        {!collapsed && (
          <div className="wb-folder-body" onWheel={stop}>
            <PlanEditor agentId={agentId} planMd={planMd} />
            <DiagramEditor agentId={agentId} mermaid={mermaid} serverError={diagramError} />
          </div>
        )}
      </div>
    </HTMLContainer>
  )
}

/** keeps the caret while the server echoes our own text back through `agent.card.upsert` */
function useServerText(incoming: string): [string, (v: string) => void] {
  const [text, setText] = useState(incoming)
  const last = useRef(incoming)
  useEffect(() => {
    if (incoming !== last.current) {
      last.current = incoming
      setText(incoming)
    }
  }, [incoming])
  return [text, setText]
}

/** `agent.plan.edit {agent_id, plan_md}`, debounced 800ms (B.6) */
function PlanEditor({ agentId, planMd }: { agentId: string; planMd: string }) {
  const [text, setText] = useServerText(planMd)
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null)
  useEffect(() => () => { if (timer.current) clearTimeout(timer.current) }, [])
  const onChange = useCallback(
    (v: string) => {
      setText(v)
      if (timer.current) clearTimeout(timer.current)
      timer.current = setTimeout(() => getWiring()?.editAgentPlan(agentId, v), EDIT_DEBOUNCE_MS)
    },
    [agentId, setText],
  )
  return (
    <section className="wb-folder-section">
      <header className="wb-folder-label">plan.md</header>
      <textarea
        className="wb-folder-text"
        spellCheck={false}
        value={text}
        aria-label={`plan for ${agentId}`}
        onPointerDown={stop}
        onKeyDown={stop}
        onChange={(e) => onChange(e.target.value)}
      />
    </section>
  )
}

/**
 * Read-only mermaid with an Edit toggle (B.6). A bad fence comes back as `edit.reject`, which
 * carries the seq we sent, so the error lands under this box and nowhere else.
 */
function DiagramEditor({ agentId, mermaid, serverError }: { agentId: string; mermaid: string; serverError: string }) {
  const [text, setText] = useServerText(mermaid)
  const [editing, setEditing] = useState(false)
  const [sentSeq, setSentSeq] = useState<number | null>(null)
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const reject = useStore((s) => s.lastReject)
  useEffect(() => () => { if (timer.current) clearTimeout(timer.current) }, [])

  const send = useCallback(
    (v: string) => {
      const seq = getWiring()?.editAgentDiagram(agentId, v)
      setSentSeq(typeof seq === 'number' ? seq : null)
    },
    [agentId],
  )
  const onChange = useCallback(
    (v: string) => {
      setText(v)
      if (timer.current) clearTimeout(timer.current)
      timer.current = setTimeout(() => send(v), EDIT_DEBOUNCE_MS)
    },
    [send, setText],
  )

  const rejected = sentSeq !== null && reject?.forSeq === sentSeq ? reject.reason : ''
  const error = rejected || serverError

  return (
    <section className="wb-folder-section">
      <header className="wb-folder-label">
        <span>diagrams.md</span>
        <button
          type="button"
          className="wb-folder-btn"
          onPointerDown={stop}
          onClick={(e) => {
            e.stopPropagation()
            setEditing((v) => !v)
          }}
          aria-label={editing ? `Stop editing the diagram of ${agentId}` : `Edit the diagram of ${agentId}`}
        >
          {editing ? 'Done' : 'Edit'}
        </button>
      </header>
      {editing ? (
        <textarea
          className="wb-folder-text wb-folder-mermaid"
          spellCheck={false}
          value={text}
          aria-label={`diagram for ${agentId}`}
          onPointerDown={stop}
          onKeyDown={stop}
          onChange={(e) => onChange(e.target.value)}
          onBlur={() => {
            if (timer.current) clearTimeout(timer.current)
            send(text)
          }}
        />
      ) : (
        <pre className="wb-folder-pre">{text || '(no diagram yet)'}</pre>
      )}
      {error && (
        <div className="wb-folder-error" role="alert">
          {error}
        </div>
      )}
    </section>
  )
}
