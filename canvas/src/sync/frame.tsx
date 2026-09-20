// Board chrome: one folder button per board frame, at the frame's top-left (rendered in page
// space through the OnTheCanvas slot, so it pans/zooms with the page) that collapses/expands
// that board.
import { useEditor, useValue, type Editor, type TLFrameShape, type TLShape, type TLShapeId } from 'tldraw'
import { getFrame, notifyFolderCollapse, setFrameCollapsed } from './apply'
import { boardFrameId, boardLayoutKey, boardOfFrameId, presentBoards, type BoardName } from './boards'
import type { LayoutPatch } from '../state/types'

/** pure: reads only the shape */
export function getShapeVisibility(shape: TLShape): 'hidden' | 'inherit' {
  return shape.meta?.hidden ? 'hidden' : 'inherit'
}

/** `(board, patch)` — the collapse flag belongs to that board's sidecar */
type CollapseListener = (board: string, patch: LayoutPatch) => void
const listeners = new Set<CollapseListener>()

/** the wiring subscribes here to persist `frames['board-<name>'].collapsed` via canvas.layout */
export function onFrameCollapse(fn: CollapseListener): () => void {
  listeners.add(fn)
  return () => listeners.delete(fn)
}

export function toggleFrame(editor: Editor, frameId: TLShapeId): void {
  const frame = getFrame(editor, frameId)
  if (!frame) return
  const next = setFrameCollapsed(editor, frameId, !frame.meta.collapsed)
  if (next) {
    const board = boardOfFrameId(String(frameId)) ?? String(frame.meta.planId)
    const patch: LayoutPatch = { frames: { [boardLayoutKey(board)]: next } }
    listeners.forEach((l) => l(board, patch))
  }
}

/**
 * B.6: the agent folder's own chevron. Same primitive as a board frame, but the patch is keyed
 * by the agent id and persisted on the folder's owner board (`apply.notifyFolderCollapse`).
 */
export function toggleFolder(editor: Editor, folderId: TLShapeId): void {
  const folder = editor.getShape(folderId)
  if (!folder) return
  notifyFolderCollapse(editor, folderId, !folder.meta.collapsed)
}

export function FrameChrome() {
  const editor = useEditor()
  const boards = useValue('board frames', () => presentBoards(editor), [editor])
  return (
    <>
      {boards.map((board) => (
        <BoardHeader key={board} editor={editor} board={board} />
      ))}
    </>
  )
}

function BoardHeader({ editor, board }: { editor: Editor; board: BoardName }) {
  const frameId = boardFrameId(board)
  const frame = useValue(`frame ${board}`, () => getFrame(editor, frameId), [editor, frameId])
  if (!frame) return null
  return <FrameButton editor={editor} frame={frame} board={board} />
}

function FrameButton({ editor, frame, board }: { editor: Editor; frame: TLFrameShape; board: BoardName }) {
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
        title={collapsed ? `Expand the ${board.toUpperCase()} board` : `Collapse the ${board.toUpperCase()} board`}
        aria-label={collapsed ? `Expand the ${board.toUpperCase()} board` : `Collapse the ${board.toUpperCase()} board`}
        onPointerDown={(e) => e.stopPropagation()}
        onClick={(e) => {
          e.stopPropagation()
          toggleFrame(editor, frame.id)
        }}
        style={{
          font: 'inherit',
          lineHeight: 1,
          padding: '3px 8px',
          border: '1px solid var(--wb-border-2)',
          borderLeft: `4px solid var(--wb-${board}-hex)`,
          borderRadius: 'var(--wb-r2)',
          background: 'var(--wb-island)',
          color: 'var(--wb-text)',
          cursor: 'pointer',
        }}
      >
        {/* the title is already on tldraw's own frame label — this is just the disclosure */}
        {collapsed ? `▸ 📁 ${frame.props.name}` : '▾ 📂'}
      </button>
    </div>
  )
}
