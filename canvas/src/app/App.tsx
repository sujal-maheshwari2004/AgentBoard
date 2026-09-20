import { useCallback, useMemo, useState } from 'react'
import { Tldraw, type Editor, type TLComponents } from 'tldraw'
import { AgentCardUtil } from '../shapes/AgentCardUtil'
import { AgentFolderUtil } from '../shapes/AgentFolderUtil'
import { PlanNodeUtil } from '../shapes/PlanNodeUtil'
import { WbShapeWrapper } from '../shapes/ShapeWrapper'
import { store } from '../state/store'
import { installZoomTracking } from '../sync/apply'
import { FrameChrome, getShapeVisibility } from '../sync/frame'
import { mountWiring } from '../sync/wire'
import { ChatBox } from '../panels/ChatBox'
import { DiagramProposal } from '../panels/DiagramProposal'
import { DispatchPopup } from '../panels/DispatchPopup'
import { EventTicker } from '../panels/EventTicker'
import { NeedsInputModal } from '../panels/NeedsInputModal'
import { RiskyEditConfirm } from '../panels/RiskyEditConfirm'
import { Toolbar } from '../panels/Toolbar'
import '../panels/panels.css'

const shapeUtils = [AgentCardUtil, AgentFolderUtil, PlanNodeUtil]
const components: TLComponents = { ContextMenu: null, OnTheCanvas: FrameChrome, ShapeWrapper: WbShapeWrapper }

export function App() {
  const [editor, setEditor] = useState<Editor | null>(null)

  const onMount = useCallback((ed: Editor) => {
    // StrictMode mounts twice: each mount gets its own wiring and the returned cleanup tears it
    // down (socket closedRef, listeners), so a late-opening socket from the first pass never leaks.
    const wiring = mountWiring(ed)
    // camera stop also decides which board covers the viewport centre → `activeBoard` (B.5)
    const stopZoom = installZoomTracking(ed, (board) => store.setActiveBoard(board))
    setEditor(ed)
    ed.setCurrentTool('select')
    return () => {
      stopZoom()
      wiring.dispose()
      setEditor((cur) => (cur === ed ? null : cur))
    }
  }, [])

  const options = useMemo(() => ({ maxPages: 1 }), [])
  // an explicit `data-theme` on <html> wins for tldraw too; otherwise it follows the OS, exactly
  // as the `prefers-color-scheme` block in tokens.css does
  const colorScheme = useMemo<'light' | 'dark' | 'system'>(() => {
    const t = document.documentElement.dataset.theme
    return t === 'dark' || t === 'light' ? t : 'system'
  }, [])

  return (
    <div className="wb-root">
      <div className="wb-canvas">
        <Tldraw
          hideUi
          colorScheme={colorScheme}
          components={components}
          shapeUtils={shapeUtils}
          getShapeVisibility={getShapeVisibility}
          onMount={onMount}
          options={options}
        />
      </div>
      <Toolbar editor={editor} />
      <EventTicker />
      <ChatBox />
      <NeedsInputModal />
      <DispatchPopup />
      <DiagramProposal />
      <RiskyEditConfirm />
    </div>
  )
}
