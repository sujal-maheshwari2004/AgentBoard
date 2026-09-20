import { useValue, type Editor } from 'tldraw'
import { store, useStore } from '../state/store'
import type { NodeStatus } from '../state/types'
import { kindOf } from '../sync/apply'
import { boardFrameId } from '../sync/boards'
import { toggleFrame } from '../sync/frame'
import { cameraAnimation } from '../sync/motion'
import { getWiring } from '../sync/wire'
import { BoardSwitcher } from './BoardSwitcher'
import { PastePlanButton } from './PastePlan'

const STATUSES: NodeStatus[] = ['todo', 'in_progress', 'blocked', 'done']

/** every status carries a spoken label, never colour alone (B.1 item 12) */
function bridgeLabel(bridge: { ok: boolean; failures: number; last_error?: string } | null): string {
  if (!bridge) return 'push bridge: status unknown'
  return `push bridge: ${bridge.ok ? 'ok' : 'failing'}, ${bridge.failures} failure(s)${bridge.last_error ? ` (${bridge.last_error})` : ''}`
}

const TOOLS: Array<{ id: string; label: string; key: string }> = [
  { id: 'select', label: 'Select', key: 'V' },
  { id: 'hand', label: 'Hand', key: 'H' },
  { id: 'geo', label: 'Box', key: 'R' },
  { id: 'arrow', label: 'Arrow', key: 'A' },
  { id: 'text', label: 'Text', key: 'T' },
  { id: 'draw', label: 'Draw', key: 'D' },
]

export function Toolbar({ editor }: { editor: Editor | null }) {
  const socket = useStore((s) => s.socket)
  const activeBoard = useStore((s) => s.activeBoard)
  const bridge = useStore((s) => s.bridge)
  const project = useStore((s) => s.project)
  const rev = useStore((s) => s.rev)
  const selectedNodeId = useValue(
    'selected plan node',
    () => {
      if (!editor) return null
      const ids = editor.getSelectedShapeIds()
      if (ids.length !== 1) return null
      const s = editor.getShape(ids[0])
      return s && kindOf(s) === 'plan-node' ? String(s.meta.planId) : null
    },
    [editor],
  )
  const currentTool = useValue('tool', () => editor?.getCurrentToolId() ?? 'select', [editor])
  const selectedNode = useStore((s) => (selectedNodeId ? s.nodes[selectedNodeId] : undefined))

  const setStatus = (status: NodeStatus) => {
    if (!selectedNodeId) return
    getWiring()?.setNodeStatus(selectedNodeId, status)
    store.note('status', `${selectedNodeId} → ${status}`)
  }

  return (
    <div className="wb-panel wb-toolbar" data-testid="toolbar">
      {TOOLS.map((t) => (
        <button
          key={t.id}
          className={`wb-btn${currentTool === t.id ? ' active' : ''}`}
          title={`${t.label} (${t.key})`}
          disabled={!editor}
          onClick={() => editor?.setCurrentTool(t.id)}
        >
          {t.label}
        </button>
      ))}
      <span className="sep" />
      <BoardSwitcher editor={editor} />
      <span className="sep" />
      <button className="wb-btn" disabled={!editor} onClick={() => getWiring()?.relayout()} title="Re-run dagre for unpinned nodes">
        Re-layout
      </button>
      <button className="wb-btn" disabled={!editor} onClick={() => editor?.zoomToFit({ animation: cameraAnimation(200) })}>
        Zoom to fit
      </button>
      <button
        className="wb-btn"
        disabled={!editor}
        onClick={() => editor && toggleFrame(editor, boardFrameId(activeBoard))}
        title={`Collapse / expand the ${activeBoard.toUpperCase()} board`}
      >
        Collapse
      </button>
      <PastePlanButton />
      <span className="sep" />
      <label className="wb-status" title="Status of the selected plan node (right-click is disabled)">
        {selectedNodeId ? (
          <>
            <span style={{ marginRight: 4 }}>{selectedNodeId}</span>
            <select
              className="wb-select"
              aria-label={`Status of ${selectedNodeId}`}
              title={`Status of ${selectedNodeId}`}
              value={selectedNode?.status ?? 'todo'}
              onChange={(e) => setStatus(e.target.value as NodeStatus)}
            >
              {STATUSES.map((s) => (
                <option key={s} value={s}>
                  {s}
                </option>
              ))}
            </select>
          </>
        ) : (
          <span style={{ color: 'var(--wb-text-3)' }}>select a node to set status</span>
        )}
      </label>
      <span className="sep" />
      <span className="wb-status" title={`socket ${socket}`}>
        <span
          className={`wb-dot ${socket === 'open' ? 'ok' : socket === 'reconnecting' || socket === 'connecting' ? 'warn' : 'bad'}`}
          role="img"
          title={`socket ${socket}`}
          aria-label={`socket ${socket}`}
        />
        socket · {socket}
      </span>
      <span className="wb-status" title={bridgeLabel(bridge)}>
        <span className={`wb-dot ${bridge ? (bridge.ok ? 'ok' : 'bad') : ''}`} role="img" title={bridgeLabel(bridge)} aria-label={bridgeLabel(bridge)} />
        bridge · {bridge ? (bridge.ok ? 'ok' : 'failing') : 'unknown'}
      </span>
      {project && (
        <span className="wb-status" title={`rev ${rev}`}>
          {project.split('/').filter(Boolean).pop()} · rev {rev}
        </span>
      )}
    </div>
  )
}
