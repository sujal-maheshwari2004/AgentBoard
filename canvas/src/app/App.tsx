// Stage 0 placeholder. T4 (canvas task) replaces this with the real app.
import { Tldraw } from 'tldraw'

export function App() {
  return (
    <div style={{ position: 'fixed', inset: 0 }}>
      <Tldraw hideUi components={{ ContextMenu: null }} />
    </div>
  )
}
