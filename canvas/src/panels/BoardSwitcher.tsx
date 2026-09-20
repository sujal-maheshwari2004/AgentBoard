// Board switcher (plan B.5): a segmented control, one segment per board, each tinted with its
// own hue (the same `--wb-hld/-lld/-er` tokens the node stripes use) and the active one filled.
// Clicking flies the camera to that board's frame; panning writes `activeBoard` back from the
// camera (see `installZoomTracking`), so the control always says where you are.
import { EASINGS, useValue, type Editor } from 'tldraw'
import { store, useStore } from '../state/store'
import { boardBounds, boardSpecsFor, presentBoards, type BoardName } from '../sync/boards'

export const BOARD_ZOOM_INSET = 64
export const BOARD_ZOOM_MS = 300

export function focusBoard(editor: Editor, board: BoardName): void {
  const bounds = boardBounds(editor, board)
  if (!bounds) return
  editor.zoomToBounds(bounds, { inset: BOARD_ZOOM_INSET, animation: { duration: BOARD_ZOOM_MS, easing: EASINGS.easeInOutCubic } })
  store.setActiveBoard(board)
}

export function BoardSwitcher({ editor }: { editor: Editor | null }) {
  const active = useStore((s) => s.activeBoard)
  const diagrams = useStore((s) => s.diagrams)
  // what is actually on the canvas wins; before the first snapshot fall back to the plan's boards
  const onCanvas = useValue('board frames', () => (editor ? presentBoards(editor) : []), [editor])
  const boards: BoardName[] = onCanvas.length ? onCanvas : boardSpecsFor(diagrams.map((d) => d.name)).map((b) => b.name)

  return (
    <div className="wb-boards" role="group" aria-label="Board">
      {boards.map((b) => (
        <button
          key={b}
          type="button"
          className={`wb-board-seg${active === b ? ' active' : ''}`}
          data-board={b}
          aria-pressed={active === b}
          title={`Go to the ${b.toUpperCase()} board`}
          disabled={!editor}
          onClick={() => editor && focusBoard(editor, b)}
        >
          {b.toUpperCase()}
        </button>
      ))}
    </div>
  )
}
