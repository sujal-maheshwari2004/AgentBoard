// Modal keyboard behaviour (B.S6 item 9): focus the dialog on open, trap Tab inside it, and
// hand Esc to the caller. Shared by every `.wb-modal`, so no modal can ship without it.
//
// Autofocus deliberately does NOT land on the first field: the diagram proposal's first control
// is a 14-row mermaid textarea, and dropping the caret in there swallows Esc-like reflexes and
// reads badly to a screen reader. The dialog itself takes focus (`tabIndex={-1}`), which is what
// makes Esc and Tab work and what announces the dialog's own label first.
import { useEffect, useRef } from 'react'

const FOCUSABLE =
  'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'

export interface ModalDialogOptions {
  /** Esc, or the backdrop's own dismiss; omitted means Esc does nothing */
  onEscape?: () => void
}

/** attach the returned ref to the dialog element (`role="dialog"`, `tabIndex={-1}`) */
export function useModalDialog<T extends HTMLElement = HTMLDivElement>({ onEscape }: ModalDialogOptions = {}) {
  const ref = useRef<T | null>(null)
  const escape = useRef(onEscape)
  escape.current = onEscape

  useEffect(() => {
    const el = ref.current
    if (!el) return
    const previous = document.activeElement as HTMLElement | null
    // focus the dialog, not its first field
    el.focus({ preventScroll: true })

    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        if (!escape.current) return
        e.preventDefault()
        e.stopPropagation()
        escape.current()
        return
      }
      if (e.key !== 'Tab') return
      const items = Array.from(el.querySelectorAll<HTMLElement>(FOCUSABLE)).filter(
        (n) => n.offsetParent !== null || n === document.activeElement,
      )
      if (items.length === 0) {
        // nothing to cycle through: keep focus on the dialog rather than letting it escape
        e.preventDefault()
        el.focus({ preventScroll: true })
        return
      }
      const first = items[0]
      const last = items[items.length - 1]
      const active = document.activeElement as HTMLElement | null
      if (!e.shiftKey && (active === last || active === el)) {
        e.preventDefault()
        first.focus()
      } else if (e.shiftKey && (active === first || active === el)) {
        e.preventDefault()
        last.focus()
      }
    }

    // capture: tldraw binds its own shortcuts on the document, and Esc there clears the selection
    el.addEventListener('keydown', onKeyDown, true)
    return () => {
      el.removeEventListener('keydown', onKeyDown, true)
      previous?.focus?.({ preventScroll: true })
    }
  }, [])

  return ref
}
