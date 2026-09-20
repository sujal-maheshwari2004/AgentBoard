// Reduced motion for the things CSS cannot reach: tldraw's camera flights (B.S6 item 13).
//
// `@media (prefers-reduced-motion: reduce)` in styles/motion.css stops every ring, dash and
// transition. A `zoomToBounds`/`zoomToSelection` animation is a JS option, so it has to ask.

export interface MotionQuery {
  matchMedia?: (query: string) => { matches: boolean }
}

export const REDUCED_MOTION_QUERY = '(prefers-reduced-motion: reduce)'

/** true when the OS asks for reduced motion; false in jsdom/SSR or when the query is unsupported */
export function prefersReducedMotion(win: MotionQuery | undefined = globalThis as MotionQuery): boolean {
  try {
    return !!win?.matchMedia?.(REDUCED_MOTION_QUERY)?.matches
  } catch {
    return false
  }
}

/**
 * Camera animation options: the requested duration normally, `0` under reduced motion (tldraw
 * treats a zero duration as an instant jump, which is exactly the accessible behaviour).
 */
export function cameraAnimation(durationMs: number, win?: MotionQuery): { duration: number } {
  return { duration: prefersReducedMotion(win) ? 0 : durationMs }
}
