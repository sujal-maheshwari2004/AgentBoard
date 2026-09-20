import React from 'react'
import ReactDOM from 'react-dom/client'
// CSS order matters: tldraw's sheet, then our tokens, then everything that consumes them
// (panels.css and the shape sheets come in through <App>).
import 'tldraw/tldraw.css'
import './styles/tokens.css'
// motion primitives (@property --wb-angle, @keyframes) before every sheet that consumes them
import './styles/motion.css'
import './styles/edges.css'
import { App } from './app/App'

// Theme hook: an explicit choice is stored as `data-theme` on <html> and wins; with no attribute
// the `prefers-color-scheme` block in tokens.css decides.
try {
  const saved = localStorage.getItem('wb-theme')
  if (saved === 'light' || saved === 'dark') document.documentElement.dataset.theme = saved
} catch {
  // private mode / blocked storage: fall back to the OS preference
}

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
)
