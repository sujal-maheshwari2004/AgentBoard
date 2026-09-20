// Plan frame chrome: a folder button at the frame's top-left (rendered in page space through the
// OnTheCanvas slot, so it pans/zooms with the page) that collapses/expands the board.
import { useEditor, useValue, type Editor, type TLFrameShape, type TLShape } from 'tldraw'
import { getFrame, setFrameCollapsed } from './apply'
import { PLAN_FRAME_NAME } from '../shapes/ids'
import type { LayoutPatch } from '../state/types'

export { PLAN_FRAME_NAME }

/** pure: reads only the shape */
export function getShapeVisibility(shape: TLShape): 'hidden' | 'inherit' {
  return shape.meta?.hidden ? 'hidden' : 'inherit'
}

type CollapseListener = (patch: LayoutPatch) => void
const listeners = new Set<CollapseListener>()

/** the wiring subscribes here to persist `frames['plan-board'].collapsed` via canvas.layout */
export function onFrameCollapse(fn: CollapseListener): () => void {
  listeners.add(fn)
  return () => listeners.delete(fn)
}

export function toggleFrame(editor: Editor): void {
  const frame = getFrame(editor)
  if (!frame) return
  const next = setFrameCollapsed(editor, !frame.meta.collapsed)
  if (next) {
    const patch: LayoutPatch = { frames: { [PLAN_FRAME_NAME]: next } }
    listeners.forEach((l) => l(patch))
  }
}

export function FrameChrome() {
  const editor = useEditor()
  const frame = useValue('plan-frame', () => getFrame(editor), [editor])
  if (!frame) return null
  return <FrameButton editor={editor} frame={frame} />
}

function FrameButton({ editor, frame }: { editor: Editor; frame: TLFrameShape }) {
  const collapsed = !!frame.meta.collapsed
  return (
    <div
      style={{
        position: 'absolute',
        left: frame.x,
        top: frame.y - 30,
        pointerEvents: 'all',
        display: 'flex',
        alignItems: 'center',
        gap: 6,
        fontFamily: 'var(--wb-font)',
        fontSize: 13,
        color: 'var(--wb-text)',
        userSelect: 'none',
      }}
    >
      <button
        type="button"
        title={collapsed ? 'Expand plan board' : 'Collapse plan board'}
        onPointerDown={(e) => e.stopPropagation()}
        onClick={(e) => {
          e.stopPropagation()
          toggleFrame(editor)
        }}
        style={{
          font: 'inherit',
          lineHeight: 1,
          padding: '3px 8px',
          border: '1px solid var(--wb-border-2)',
          borderRadius: 'var(--wb-r2)',
          background: 'var(--wb-island)',
          color: 'var(--wb-text)',
          cursor: 'pointer',
        }}
      >
        {collapsed ? '▸ 📁' : '▾ 📂'} {frame.props.name}
      </button>
    </div>
  )
}
