// Pure half of the diagram-proposal modal (plan B.10): the Changes block a `diagram.request`
// renders as, and the `diagram.reply` payload the buttons send. Kept out of the `.tsx` so
// tests/diagramproposal.test.ts can exercise it without React or tldraw.
import type { ClientPayloads, DiagramEdgeRef, DiagramNodeRef, DiagramRequest } from '../state/types'
import { boardSpec } from '../sync/boards'

/** one row of the Changes block; `sign` and `tone` are the only visual encoding it carries */
export interface ChangeLine {
  key: string
  sign: '+' | '−'
  tone: 'added' | 'removed'
  what: 'node' | 'edge'
  /** the node id, or `src → dst` for an edge */
  text: string
  /** the node label or the edge label; `''` when the payload has none */
  detail: string
}

/** `'LLD · Components'` for a known board, else the raw name (a board we do not draw yet) */
export function boardTitle(name: string): string {
  return boardSpec(name)?.title ?? name
}

export function edgeText(e: DiagramEdgeRef): string {
  return `${e.src} → ${e.dst}`
}

function nodeLine(n: DiagramNodeRef, tone: 'added' | 'removed'): ChangeLine {
  return { key: `${tone}-node-${n.id}`, sign: tone === 'added' ? '+' : '−', tone, what: 'node', text: n.id, detail: n.label ?? '' }
}

function edgeLine(e: DiagramEdgeRef, tone: 'added' | 'removed'): ChangeLine {
  return { key: `${tone}-edge-${e.src}-${e.dst}`, sign: tone === 'added' ? '+' : '−', tone, what: 'edge', text: edgeText(e), detail: e.label ?? '' }
}

/**
 * The Changes block, in the order B.10 draws it: added nodes, removed nodes, added edges,
 * removed edges. Empty for a proposal that changes nothing (the modal then says so in words).
 */
export function diagramChangeLines(req: DiagramRequest): ChangeLine[] {
  return [
    ...(req.nodes_added ?? []).map((n) => nodeLine(n, 'added')),
    ...(req.nodes_removed ?? []).map((id) => nodeLine({ id }, 'removed')),
    ...(req.edges_added ?? []).map((e) => edgeLine(e, 'added')),
    ...(req.edges_removed ?? []).map((e) => edgeLine(e, 'removed')),
  ]
}

/** one-line summary of the diff, e.g. `+2 nodes · −1 node · +2 edges` */
export function changeSummary(req: DiagramRequest): string {
  const parts: string[] = []
  const add = (n: number, sign: string, unit: string) => {
    if (n) parts.push(`${sign}${n} ${unit}${n === 1 ? '' : 's'}`)
  }
  add((req.nodes_added ?? []).length, '+', 'node')
  add((req.nodes_removed ?? []).length, '−', 'node')
  add((req.edges_added ?? []).length, '+', 'edge')
  add((req.edges_removed ?? []).length, '−', 'edge')
  return parts.join(' · ')
}

/**
 * Does the owner's text differ from what was proposed? Whitespace-only differences do not
 * count — the server applies exactly this rule when it decides whether the reply was `edited`.
 */
export function mermaidEdited(proposed: string, text: string): boolean {
  const owner = (text ?? '').trim()
  return owner.length > 0 && owner !== (proposed ?? '').trim()
}

/**
 * B.10: `mermaid` rides along only when the textarea differs from the proposal, and then the
 * owner's text wins server-side. An empty note is omitted rather than sent blank.
 */
export function diagramReplyPayload(
  req: Pick<DiagramRequest, 'request_id' | 'mermaid'>,
  opts: { approved: boolean; note?: string; text?: string },
): ClientPayloads['diagram.reply'] {
  const payload: ClientPayloads['diagram.reply'] = { request_id: req.request_id, approved: opts.approved }
  const note = (opts.note ?? '').trim()
  if (note) payload.note = note
  if (mermaidEdited(req.mermaid, opts.text ?? '')) payload.mermaid = (opts.text ?? '').trim()
  return payload
}

// ---- grouping a large diff (B.S6 item 10) ----

/**
 * Past this many rows the Changes block stops being a flat list inside a 22vh scroller (14 rows
 * was observed in B.S5) and becomes four collapsible groups.
 */
export const CHANGES_GROUP_THRESHOLD = 8
/** a group with more rows than this starts collapsed */
export const CHANGES_GROUP_OPEN_MAX = 6

export interface ChangeGroup {
  key: string
  /** `'3 nodes added'`, `'1 edge removed'`, … */
  title: string
  tone: 'added' | 'removed'
  what: 'node' | 'edge'
  lines: ChangeLine[]
  /** whether this group is expanded when the modal opens */
  defaultOpen: boolean
}

const GROUP_ORDER: Array<{ tone: 'added' | 'removed'; what: 'node' | 'edge' }> = [
  { tone: 'added', what: 'node' },
  { tone: 'removed', what: 'node' },
  { tone: 'added', what: 'edge' },
  { tone: 'removed', what: 'edge' },
]

/** the four B.10 buckets, in draw order, empty ones dropped */
export function groupChangeLines(lines: readonly ChangeLine[]): ChangeGroup[] {
  const groups: ChangeGroup[] = []
  for (const o of GROUP_ORDER) {
    const rows = lines.filter((l) => l.tone === o.tone && l.what === o.what)
    if (!rows.length) continue
    groups.push({
      key: `${o.tone}-${o.what}`,
      title: `${rows.length} ${o.what}${rows.length === 1 ? '' : 's'} ${o.tone}`,
      tone: o.tone,
      what: o.what,
      lines: rows,
      defaultOpen: rows.length <= CHANGES_GROUP_OPEN_MAX,
    })
  }
  return groups
}

/** a flat list is fine up to the threshold; a big diff is grouped */
export function shouldGroupChanges(lines: readonly ChangeLine[]): boolean {
  return lines.length > CHANGES_GROUP_THRESHOLD
}
