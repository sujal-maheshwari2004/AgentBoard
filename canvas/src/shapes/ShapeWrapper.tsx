// tldraw renders every shape inside a wrapper div. Overriding it is the supported way to get our
// own attributes onto that div, which is how `styles/edges.css` paints an arrow from its `meta`
// without touching the arrow shape, its bindings or their ids (collect.ts reads those).
import { forwardRef } from 'react'
import { DefaultShapeWrapper, type TLShapeWrapperProps } from 'tldraw'
import { edgeState } from '../sync/apply'

export const WbShapeWrapper = forwardRef<HTMLDivElement, TLShapeWrapperProps>(function WbShapeWrapper(props, ref) {
  const meta = props.shape.meta as { kind?: string; srcStatus?: string; dstStatus?: string } | undefined
  const extra: Record<string, string> = {}
  if (meta?.kind === 'edge') {
    extra['data-wb-edge'] = edgeState(String(meta.srcStatus ?? 'todo'), String(meta.dstStatus ?? 'todo'))
  }
  return <DefaultShapeWrapper ref={ref} {...props} {...extra} />
})
