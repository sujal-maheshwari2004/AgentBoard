// Island geometry shared between React and CSS (B.S6 items 3 and 4).
//
// The inspector used to hard-code `bottom: 436 | 60`, two numbers derived by hand from the chat
// island's 420px height plus an 8px gutter. Both now come from here: React writes the chat
// island's height into `--wb-chat-h` on `<html>` and the inspector's CSS stacks on
// `calc(var(--wb-chat-h) + var(--wb-s6))`, so the two cannot drift.

/** expanded chat island height (B.8: 360 x 420) */
export const CHAT_ISLAND_H = 420
/** the collapsed pill (B.9) */
export const CHAT_PILL_H = 44
/** the CSS custom property both panels read */
export const CHAT_HEIGHT_VAR = '--wb-chat-h'

export function chatIslandHeight(open: boolean): number {
  return open ? CHAT_ISLAND_H : CHAT_PILL_H
}

export function chatHeightValue(open: boolean): string {
  return `${chatIslandHeight(open)}px`
}

/** the inspector's collapsible sections (B.S6 item 3) */
export type InspectorSection = 'feed' | 'plan' | 'diagram'

export const INSPECTOR_SECTIONS: readonly InspectorSection[] = ['feed', 'plan', 'diagram']

/**
 * Which sections start open. One scrolling column with both editors populated squeezed the
 * activity feed to about one row (B.S4 hand-off), so the feed — the thing you open the inspector
 * for — is the only section expanded on first open; the editors are one click away.
 */
export function defaultOpenSections(): Record<InspectorSection, boolean> {
  return { feed: true, plan: false, diagram: false }
}

export function toggleSection(
  open: Record<InspectorSection, boolean>,
  section: InspectorSection,
): Record<InspectorSection, boolean> {
  return { ...open, [section]: !open[section] }
}
