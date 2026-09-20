// tldraw renders every shape inside a wrapper div. Overriding it is the supported way to get our
// own attributes onto that div, which is how `styles/edges.css` paints an arrow from its `meta`
// without touching the arrow shape, its bindings or their ids (collect.ts reads those).
//
// B.S6 item 12: the wrapper is also where an edge gets its spoken description. An arrow carries
// its whole meaning in colour and dash pattern, so without this an edge is invisible to a screen
// reader and to anyone who cannot tell amber from grey.
import { forwardRef } from 'react'
import { DefaultShapeWrapper, type TLShapeWrapperProps } from 'tldraw'
import { edgeState, type EdgeState } from '../sync/apply'

const EDGE_MEANING: Record<EdgeState, string> = {
  default: 'not started',
  working: 'source running',
  done: 'dependency satisfied',
  blocked: 'target blocked',
}

/** `dependency node-a → node-b — dependency satisfied, cross-board` (`planId` is `edgeKey`) */
export function edgeDescription(planId: string, state: EdgeState, cross: boolean): string {
  const [src, dst] = planId.split('__')
  const pair = src && dst ? `${src} → ${dst}` : planId || 'dependency'
  return `dependency ${pair} — ${EDGE_MEANING[state]}${cross ? ', cross-board' : ''}`
}

export const WbShapeWrapper = forwardRef<HTMLDivElement, TLShapeWrapperProps>(function WbShapeWrapper(props, ref) {
  const meta = props.shape.meta as
    | { kind?: string; planId?: string; srcStatus?: string; dstStatus?: string; cross?: boolean }
    | undefined
  const extra: Record<string, string> = {}
  if (meta?.kind === 'edge') {
    const state = edgeState(String(meta.srcStatus ?? 'todo'), String(meta.dstStatus ?? 'todo'))
    extra['data-wb-edge'] = state
    // B.5: the endpoints sit on different boards — same mechanism, one extra attribute
    if (meta.cross) extra['data-wb-cross'] = '1'
    const described = edgeDescription(String(meta.planId ?? ''), state, !!meta.cross)
    extra['aria-label'] = described
    extra['title'] = described
    extra['role'] = 'img'
  }
  return <DefaultShapeWrapper ref={ref} {...props} {...extra} />
})
