import { useCallback, useMemo, useState } from 'react'
import { Tldraw, type Editor, type TLComponents } from 'tldraw'
import { AgentCardUtil } from '../shapes/AgentCardUtil'
import { FrameChrome, getShapeVisibility } from '../sync/frame'
import { mountWiring } from '../sync/wire'
import { ChatBox } from '../panels/ChatBox'
import { DispatchPopup } from '../panels/DispatchPopup'
import { EventTicker } from '../panels/EventTicker'
import { NeedsInputModal } from '../panels/NeedsInputModal'
import { RiskyEditConfirm } from '../panels/RiskyEditConfirm'
import { Toolbar } from '../panels/Toolbar'
import '../panels/panels.css'

const shapeUtils = [AgentCardUtil]
const components: TLComponents = { ContextMenu: null, OnTheCanvas: FrameChrome }

export function App() {
  const [editor, setEditor] = useState<Editor | null>(null)

  const onMount = useCallback((ed: Editor) => {
    // StrictMode mounts twice: each mount gets its own wiring and the returned cleanup tears it
    // down (socket closedRef, listeners), so a late-opening socket from the first pass never leaks.
    const wiring = mountWiring(ed)
    setEditor(ed)
    ed.setCurrentTool('select')
    return () => {
      wiring.dispose()
      setEditor((cur) => (cur === ed ? null : cur))
    }
  }, [])

  const options = useMemo(() => ({ maxPages: 1 }), [])

  return (
    <div className="wb-root">
      <div className="wb-canvas">
        <Tldraw
          hideUi
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
      <RiskyEditConfirm />
    </div>
  )
}
