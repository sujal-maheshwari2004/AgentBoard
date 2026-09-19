import { describe, expect, it } from 'vitest'
import {
  agentShapeId,
  bindingId,
  edgeShapeId,
  frameShapeId,
  nodeShapeId,
  parseEdgeKey,
  parseShapeId,
  provisionalNodeId,
  shapeMeta,
  slugify,
} from '../src/shapes/ids'

describe('shape ids', () => {
  it('derives stable ids by namespace prefix', () => {
    expect(nodeShapeId('node-parser')).toBe('shape:n_node-parser')
    expect(agentShapeId('agent-parser')).toBe('shape:a_agent-parser')
    expect(frameShapeId('plan-board')).toBe('shape:f_plan-board')
    expect(frameShapeId()).toBe('shape:f_plan-board')
    expect(edgeShapeId('node-a', 'node-b')).toBe('shape:e_node-a__node-b')
    expect(bindingId('node-a', 'node-b', 'start')).toBe('binding:b_node-a__node-b_start')
    expect(bindingId('node-a', 'node-b', 'end')).toBe('binding:b_node-a__node-b_end')
  })

  it('parses ids back to kind + plan id', () => {
    expect(parseShapeId(nodeShapeId('node-parser'))).toEqual({ kind: 'plan-node', id: 'node-parser' })
    expect(parseShapeId(agentShapeId('agent-x'))).toEqual({ kind: 'agent-card', id: 'agent-x' })
    expect(parseShapeId(frameShapeId('plan-board'))).toEqual({ kind: 'plan-frame', id: 'plan-board' })
    expect(parseShapeId(edgeShapeId('node-a', 'node-b'))).toEqual({ kind: 'edge', id: 'node-a__node-b', src: 'node-a', dst: 'node-b' })
  })

  it('returns null for foreign ids', () => {
    expect(parseShapeId('shape:abc123')).toBeNull()
    expect(parseShapeId('shape:x_foo')).toBeNull()
    expect(parseShapeId('binding:b_a__b_start')).toBeNull()
    expect(parseShapeId('shape:e_nounderscore')).toBeNull()
    expect(parseShapeId('shape:n')).toBeNull()
  })

  it('round-trips every namespace', () => {
    for (const [make, kind, id] of [
      [nodeShapeId, 'plan-node', 'node-a-b-c'],
      [agentShapeId, 'agent-card', 'agent-1'],
      [frameShapeId, 'plan-frame', 'plan-board'],
    ] as const) {
      expect(parseShapeId(make(id))).toMatchObject({ kind, id })
    }
  })

  it('parses edge keys', () => {
    expect(parseEdgeKey('node-a__node-b')).toEqual({ src: 'node-a', dst: 'node-b' })
    expect(parseEdgeKey('node-a')).toBeNull()
    expect(parseEdgeKey('__x')).toBeNull()
    expect(parseEdgeKey('x__')).toBeNull()
  })

  it('stamps meta with kind and planId', () => {
    expect(shapeMeta('plan-node', 'node-a')).toEqual({ kind: 'plan-node', planId: 'node-a' })
    expect(shapeMeta('edge', 'a__b', { diagram: 'hld' })).toEqual({ kind: 'edge', planId: 'a__b', diagram: 'hld' })
  })

  it('slugifies labels into provisional node ids', () => {
    expect(slugify('Mermaid Parser v2')).toBe('mermaid-parser-v2')
    expect(slugify('  --Hello, World!--  ')).toBe('hello-world')
    expect(slugify('')).toBe('untitled')
    expect(provisionalNodeId('Files layer')).toBe('node-files-layer')
    expect(provisionalNodeId('Files layer')).toMatch(/^node-[a-z0-9][a-z0-9-]*$/)
  })
})
