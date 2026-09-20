// B.10's diagram-proposal modal, as pure functions: what the Changes block renders from a
// `diagram.request`, when the owner's mermaid rides along with the reply, and how a re-sent
// proposal replaces the pending one.
import { describe, expect, it } from 'vitest'
import { boardTitle, changeSummary, diagramChangeLines, diagramReplyPayload, mermaidEdited } from '../src/panels/diagramChanges'
import { initialState, reduce } from '../src/state/store'
import type { DiagramRequest, ServerMessage } from '../src/state/types'

const MERMAID = 'flowchart TD\n    node-parser["Mermaid parser"]\n    node-store["Plan store"]\n    node-parser --> node-store\n'

function request(over: Partial<DiagramRequest> = {}): DiagramRequest {
  return {
    request_id: 'a1b2c3d4',
    name: 'lld',
    mermaid: MERMAID,
    rationale: 'the parser and store split along the file boundary',
    nodes_added: [{ id: 'node-parser', label: 'Mermaid parser' }],
    nodes_removed: ['node-legacy'],
    edges_added: [{ src: 'node-parser', dst: 'node-store', label: 'feeds' }],
    edges_removed: [{ src: 'node-legacy', dst: 'node-store', label: null }],
    ...over,
  }
}

function msg(payload: DiagramRequest, seq = 1_000_001): ServerMessage {
  return { type: 'diagram.request', payload, seq, ts: '2026-09-20T00:00:00.000Z', replyTo: null }
}

describe('the Changes block', () => {
  it('renders additions and removals of both kinds, in B.10 order', () => {
    const lines = diagramChangeLines(request())
    expect(lines.map((l) => [l.sign, l.what, l.text, l.detail])).toEqual([
      ['+', 'node', 'node-parser', 'Mermaid parser'],
      ['−', 'node', 'node-legacy', ''],
      ['+', 'edge', 'node-parser → node-store', 'feeds'],
      ['−', 'edge', 'node-legacy → node-store', ''],
    ])
    // the tone is what colours the row: additions --wb-done, removals --wb-blocked
    expect(lines.map((l) => l.tone)).toEqual(['added', 'removed', 'added', 'removed'])
    expect(new Set(lines.map((l) => l.key)).size).toBe(4)
  })

  it('is empty when the proposal changes nothing, so the modal can say so in words', () => {
    const empty = request({ nodes_added: [], nodes_removed: [], edges_added: [], edges_removed: [] })
    expect(diagramChangeLines(empty)).toEqual([])
    expect(changeSummary(empty)).toBe('')
  })

  it('summarises the diff and pluralises per kind', () => {
    expect(changeSummary(request())).toBe('+1 node · −1 node · +1 edge · −1 edge')
    expect(changeSummary(request({ nodes_added: [{ id: 'a' }, { id: 'b' }], nodes_removed: [], edges_removed: [] }))).toBe('+2 nodes · +1 edge')
  })

  it('titles the modal with the board name, falling back to an unknown board id', () => {
    expect(boardTitle('lld')).toBe('LLD · Components')
    expect(boardTitle('hld')).toBe('HLD · System')
    expect(boardTitle('api')).toBe('api')
  })
})

describe('diagram.reply', () => {
  it('omits mermaid when the textarea still holds the proposal (whitespace does not count)', () => {
    const req = request()
    expect(diagramReplyPayload(req, { approved: true, text: req.mermaid })).toEqual({ request_id: 'a1b2c3d4', approved: true })
    expect(diagramReplyPayload(req, { approved: true, text: `\n${req.mermaid}   ` })).toEqual({ request_id: 'a1b2c3d4', approved: true })
    expect(mermaidEdited(req.mermaid, req.mermaid)).toBe(false)
  })

  it('sends the owner-edited text, trimmed, when it differs', () => {
    const req = request()
    const edited = `${MERMAID}    node-store --> node-out\n`
    expect(diagramReplyPayload(req, { approved: true, note: ' looks right ', text: edited })).toEqual({
      request_id: 'a1b2c3d4',
      approved: true,
      note: 'looks right',
      mermaid: edited.trim(),
    })
    expect(mermaidEdited(req.mermaid, edited)).toBe(true)
  })

  it('never sends an empty note or an emptied textarea', () => {
    const req = request()
    expect(diagramReplyPayload(req, { approved: false, note: '   ', text: '' })).toEqual({ request_id: 'a1b2c3d4', approved: false })
    expect(mermaidEdited(req.mermaid, '   ')).toBe(false)
  })

  it('rejects with the note the owner typed', () => {
    expect(diagramReplyPayload(request(), { approved: false, note: 'ER first', text: MERMAID })).toEqual({
      request_id: 'a1b2c3d4',
      approved: false,
      note: 'ER first',
    })
  })
})

describe('pending proposals in the store', () => {
  it('queues one entry per request and keeps the order they arrived in', () => {
    let s = reduce(initialState(), msg(request()))
    s = reduce(s, msg(request({ request_id: 'ffff0000', name: 'er' }), 1_000_002))
    expect(s.diagramRequests.map((d) => d.request_id)).toEqual(['a1b2c3d4', 'ffff0000'])
    expect(s.ticker.map((t) => t.kind)).toEqual(['diagram', 'diagram'])
    expect(s.ticker.at(-1)?.text).toBe('diagram proposed: er (req ffff0000)')
  })

  it('replaces the entry on a client.hello re-send, so a recomputed diff and an error land', () => {
    let s = reduce(initialState(), msg(request()))
    s = reduce(s, msg(request({ nodes_added: [], nodes_removed: [], edges_added: [], edges_removed: [], error: 'unknown node node-legacy' }), 1_000_003))
    expect(s.diagramRequests).toHaveLength(1)
    expect(s.diagramRequests[0].error).toBe('unknown node node-legacy')
    expect(diagramChangeLines(s.diagramRequests[0])).toEqual([])
  })

  it('keeps the proposal pending when the server answers bad_diagram (it is still approvable)', () => {
    let s = reduce(initialState(), msg(request()))
    s = reduce(s, { type: 'server.error', payload: { forSeq: 7, code: 'bad_diagram', message: 'cycle: a → b → a' }, seq: 1_000_004, ts: 't', replyTo: null })
    expect(s.diagramRequests).toHaveLength(1)
    // the modal matches the error to its own reply by send seq
    expect(s.lastError).toMatchObject({ code: 'bad_diagram', forSeq: 7 })
  })
})
