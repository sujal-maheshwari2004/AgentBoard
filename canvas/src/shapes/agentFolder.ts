// Pure half of the agent folder (plan B.6): geometry, the collapse patch that reaches the
// layout sidecar, and the two edit payloads the folder's editors send. Kept out of the
// `ShapeUtil` module so tests (and `sync/apply.ts`) can use it without importing tldraw's
// renderer or the wiring — and so `apply.ts ← AgentFolderUtil ← wire.ts` never forms a cycle.
import type { AgentStatus, ClientPayloads, LayoutFrame } from '../state/types'

/** B.6 default geometry */
export const FOLDER_W = 320
export const FOLDER_H = 240
/** the ComfyUI title strip (30px there, 36px here so the chevron and pill fit) */
export const FOLDER_HEADER_H = 36
/** collapsed the folder is just its header */
export const FOLDER_COLLAPSED_H = 36
export const FOLDER_MIN_W = 220
export const FOLDER_MIN_H = 120
/** debounce before a keystroke becomes `agent.plan.edit` / `agent.diagram.edit` */
export const EDIT_DEBOUNCE_MS = 800

/** hue = the agent's status colour, so a working folder reads amber at a glance (B.6) */
export const FOLDER_HUE: Record<AgentStatus, string> = {
  idle: 'var(--wb-idle)',
  working: 'var(--wb-working)',
  blocked: 'var(--wb-blocked)',
  done: 'var(--wb-done)',
}

export function folderHue(status: AgentStatus | string | null | undefined): string {
  return FOLDER_HUE[(status ?? 'idle') as AgentStatus] ?? FOLDER_HUE.idle
}

/**
 * The sidecar entry for a folder's collapse state. `h` is always the EXPANDED height, so
 * expanding later restores the folder to the size it had — the same contract `apply.ts` uses
 * for board frames (`frames['board-hld']`); here the key is the agent id (`frames['agent-parser']`).
 */
export function folderCollapsePatch(
  folder: { x: number; y: number; w: number; h: number; expandedH?: number | null },
  collapsed: boolean,
): LayoutFrame {
  // Always called with the shape as it is BEFORE the toggle: collapsing records the height we
  // are leaving, expanding records the height we are restoring from `meta.expandedH`.
  const expandedH = collapsed ? folder.h : (folder.expandedH ?? folder.h)
  return { x: folder.x, y: folder.y, w: folder.w, h: expandedH, collapsed }
}

export function planEditPayload(agentId: string, planMd: string): ClientPayloads['agent.plan.edit'] {
  return { agent_id: agentId, plan_md: planMd }
}

export function diagramEditPayload(agentId: string, mermaid: string): ClientPayloads['agent.diagram.edit'] {
  return { agent_id: agentId, mermaid }
}

/** the first mermaid fence the folder shows, read off the server-parsed diagram or the raw file */
export function folderMermaid(agent: { diagram?: { mermaid?: string } | null; diagrams_md?: string }): string {
  const parsed = agent.diagram?.mermaid
  if (parsed && parsed.trim()) return parsed
  return extractFence(agent.diagrams_md ?? '')
}

/** first ```mermaid``` (or plain) fence of a markdown document, or '' when there is none */
export function extractFence(md: string): string {
  const m = /```[^\n]*\n([\s\S]*?)```/.exec(md)
  return m ? m[1].replace(/\s+$/, '') : ''
}
