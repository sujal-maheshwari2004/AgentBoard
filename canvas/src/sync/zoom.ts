// Zoom adaptation (plan B.3 / B.1 item 11).
//
// `--wb-zoom` is written on the editor container at camera *stop* (never per frame), and the same
// value is published through a signal so shapes can degrade to a dot when the box would be a few
// pixels wide. Border alpha ramps 0.1 -> 0.7 as zoom goes 1.0 -> 0.2 with gamma 2.2 (n8n), so
// nodes stay legible zoomed out.
import { atom } from 'tldraw'

/** current zoom level, updated on camera stop */
export const zoomAtom = atom('wb-zoom', 1)

export const ZOOM_ALPHA_MIN = 0.1
export const ZOOM_ALPHA_MAX = 0.7
export const ZOOM_ALPHA_GAMMA = 2.2
/** below this the node renders as a 10px status dot with no text (Dagster's degrade rule) */
export const DEGRADE_ZOOM = 0.25

function clamp01(n: number): number {
  return n < 0 ? 0 : n > 1 ? 1 : n
}

/** 0.1 at zoom >= 1.0, 0.7 at zoom <= 0.2, gamma 2.2 in between */
export function borderAlpha(zoom: number): number {
  const t = clamp01((1 - zoom) / 0.8)
  return ZOOM_ALPHA_MIN + (ZOOM_ALPHA_MAX - ZOOM_ALPHA_MIN) * Math.pow(t, ZOOM_ALPHA_GAMMA)
}
