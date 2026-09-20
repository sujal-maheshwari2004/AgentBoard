#!/usr/bin/env node
// Mock whiteboard server for canvas development (CONTRACTS §6).
//   pnpm mock            → ws + static dist/ on MOCK_PORT (default 43999)
//   WHITEBOARD_PORT=43999 pnpm dev   → vite proxies /ws to it
// Replays tests/fixtures/protocol/snapshot.json on client.hello, then scripted.json
// ({delayMs, message}[]), and answers client messages the way the real server would.
import { createServer } from 'node:http'
import { readFileSync, existsSync, statSync } from 'node:fs'
import { join, extname, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'
import { WebSocketServer } from 'ws'

const here = dirname(fileURLToPath(import.meta.url))
const PORT = Number(process.env.MOCK_PORT ?? 43999)
const DIST = join(here, 'dist')
const FIXTURES = join(here, 'tests', 'fixtures', 'protocol')

const snapshotFixture = JSON.parse(readFileSync(join(FIXTURES, 'snapshot.json'), 'utf8'))
const scripted = JSON.parse(readFileSync(join(FIXTURES, 'scripted.json'), 'utf8'))

// ---- in-memory truth (mutated by client ops so re-connects see the latest state) ----
const state = structuredClone(snapshotFixture)
let eventSeq = 40
const events = []
const pendingRisky = new Map() // request_id -> {ops, forSeq, ws}
const pendingDiagrams = new Map() // request_id -> {name, mermaid, rationale} (B.10 / A.2)

const now = () => new Date().toISOString()
const iso = (ms) => new Date(ms).toISOString()
const log = (...a) => console.log(new Date().toISOString().slice(11, 23), ...a)

const MIME = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.mjs': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.json': 'application/json',
  '.map': 'application/json',
  '.svg': 'image/svg+xml',
  '.png': 'image/png',
  '.woff': 'font/woff',
  '.woff2': 'font/woff2',
  '.ico': 'image/x-icon',
}

const http = createServer((req, res) => {
  const url = new URL(req.url ?? '/', 'http://localhost')
  if (url.pathname === '/api/health') {
    res.writeHead(200, { 'content-type': 'application/json' })
    res.end(JSON.stringify({ ok: true, mock: true, rev: state.rev, pending_diagrams: pendingDiagrams.size }))
    return
  }
  // B.10 on demand: /api/mock/diagram?kind=good|bad[&name=lld] publishes one `diagram.request`.
  // `kind=bad` proposes mermaid the server refuses, so the modal's error path is really exercised.
  if (url.pathname === '/api/mock/diagram') {
    const kind = url.searchParams.get('kind') ?? 'good'
    const name = url.searchParams.get('name') ?? 'lld'
    const payload = proposeDiagram(
      name,
      kind === 'bad' ? BAD_PROPOSAL : GOOD_PROPOSAL,
      kind === 'bad'
        ? 'the parser needs its own component box (this proposal is deliberately malformed)'
        : 'the parser and store split along the file boundary, so each gets its own component box',
    )
    res.writeHead(200, { 'content-type': 'application/json' })
    res.end(JSON.stringify(payload))
    return
  }
  if (!existsSync(DIST)) {
    res.writeHead(503, { 'content-type': 'text/plain' })
    res.end('canvas/dist missing: run `pnpm build`, or use `pnpm dev` with WHITEBOARD_PORT=' + PORT)
    return
  }
  let file = join(DIST, url.pathname === '/' ? 'index.html' : url.pathname)
  if (!file.startsWith(DIST) || !existsSync(file) || statSync(file).isDirectory()) file = join(DIST, 'index.html') // SPA fallback
  res.writeHead(200, { 'content-type': MIME[extname(file)] ?? 'application/octet-stream' })
  res.end(readFileSync(file))
})

const wss = new WebSocketServer({ server: http, path: '/ws' })

const clients = new Set()

function envelopeFor(client, type, payload, { event = false, replyTo = null } = {}) {
  const seq = event ? ++eventSeq : ++client.counter
  return { type, payload, seq, ts: now(), replyTo }
}

function sendTo(client, type, payload, opts) {
  if (client.ws.readyState !== 1) return
  const env = envelopeFor(client, type, payload, opts)
  client.ws.send(JSON.stringify(env))
  return env.seq
}

/** broadcast a non-event message to every client (each with its own counter) */
function broadcast(type, payload) {
  for (const c of clients) sendTo(c, type, payload)
}

/** append an event and broadcast it with the event seq */
function appendEvent({ agent_id = 'server', node_id = null, type, note = '', notified = [], data = {} }) {
  const ev = { seq: ++eventSeq, ts: now(), agent_id, node_id, type, note, notified, data }
  events.push(ev)
  for (const c of clients) {
    if (c.ws.readyState !== 1) continue
    c.ws.send(JSON.stringify({ type: 'event.append', payload: ev, seq: ev.seq, ts: ev.ts, replyTo: null }))
  }
  // the Bus projects every `chat` event into exactly one `chat.message` (A.4); the client
  // dedupes on `id`, which is what makes the hello backfill safe
  const projected = chatMessageFromEvent(ev)
  if (projected) {
    chatMessages.push(projected)
    broadcast('chat.message', projected)
  }
  return ev
}

// ---- chat threading (A.4 / B.9): one `chat.message` projected per `chat` event ----

const CHAT_BACKFILL_PER_THREAD = 50
const chatMessages = []

/** mirror of `whiteboard/server/chat.py::thread_for` */
function threadFor(sender, to, explicit) {
  if (explicit && String(explicit).trim()) return String(explicit).trim()
  sender = (sender ?? '').trim()
  to = (to ?? '').trim()
  if (sender === 'root' || sender === 'server') return 'root'
  if (sender && sender !== 'user') return sender
  return to && to !== 'user' ? to : 'root'
}

/** mirror of `chat_message_from_event`; returns null for anything that is not a chat event */
function chatMessageFromEvent(ev) {
  if (ev.type !== 'chat') return null
  const d = ev.data ?? {}
  const from = String(d.from ?? ev.agent_id ?? '')
  const to = d.to ?? null
  return {
    id: `chat-${ev.seq}`,
    thread: threadFor(from, to, d.thread),
    from,
    to: to ? String(to) : null,
    text: ev.note,
    ts: ev.ts,
    seq: ev.seq,
    reply_to: d.reply_to ? String(d.reply_to) : null,
    node_id: ev.node_id,
  }
}

/** the last N per thread, oldest first — what `client.hello` replays before the events */
function chatBackfill(perThread = CHAT_BACKFILL_PER_THREAD) {
  const byThread = new Map()
  for (const m of chatMessages) {
    if (!byThread.has(m.thread)) byThread.set(m.thread, [])
    byThread.get(m.thread).push(m)
  }
  const out = []
  for (const list of byThread.values()) out.push(...list.slice(-perThread))
  out.sort((a, b) => a.seq - b.seq)
  return out
}

/** history so a first load already has two threads to page through */
function seedChat() {
  const lines = [
    { agent_id: 'user', note: 'can we merge parser and files into one node?', data: { from: 'user', to: 'root', thread: 'root' } },
    { agent_id: 'root', note: 'no — files is already done and parser depends on it; merging would redo work', data: { from: 'root', to: 'user', thread: 'root' } },
    { agent_id: 'user', note: 'use the fixture corpus in tests/fixtures for the fence tests', data: { from: 'user', to: 'agent-parser', thread: 'agent-parser' } },
    { agent_id: 'agent-parser', note: 'ack — switching the tokenizer tests onto it now', data: { from: 'agent-parser', to: 'user', thread: 'agent-parser' } },
  ]
  for (const l of lines) appendEvent({ ...l, type: 'chat', node_id: null })
}

/**
 * The fixture's live agents are rebased onto the wall clock at boot, so `⏱ 1m 23s` on the card
 * reads as a real elapsed time rather than "six months" (B.7's shared `nowMs` clock).
 */
function rebaseAgent(a) {
  const t = Date.now()
  if (!a.spawned_at) return a
  if (a.finished_at) {
    // a finished agent keeps its frozen duration, moved to "a few minutes ago"
    const dur = Math.max(1000, Date.parse(a.finished_at) - Date.parse(a.spawned_at))
    a.spawned_at = iso(t - dur - 5 * 60_000)
    a.heartbeat_at = iso(t - 5 * 60_000 - 2000)
    a.finished_at = iso(t - 5 * 60_000)
  } else {
    const dur = Math.max(1000, (a.metrics?.elapsed_s ?? 83) * 1000)
    a.spawned_at = iso(t - dur)
    a.heartbeat_at = iso(t - 3000)
  }
  return a
}

function rebaseAgents() {
  for (const a of state.agents) rebaseAgent(a)
}
rebaseAgents()
seedChat()

const ACTIVITIES = [
  'writing the parser fence tests',
  'reading whiteboard/mermaid.py',
  'running uv run pytest -q tests/test_mermaid.py',
  'refactoring serialize() for edge labels',
  'updating docs/CONTRACTS.md §4',
]
let beat = 0

/** every 5s: bump one working agent's metrics and push a fresh card (A.5 `touch_agent`) */
function heartbeat() {
  const live = state.agents.filter((a) => a.status === 'working' && !a.finished_at)
  if (!live.length || !clients.size) return
  beat++
  for (const a of live) {
    const m = (a.metrics ??= {})
    m.tool_calls = (m.tool_calls ?? 0) + 1
    m.tokens_in = (m.tokens_in ?? 0) + 780
    m.tokens_out = (m.tokens_out ?? 0) + 190
    m.cost_usd = Number(((m.cost_usd ?? 0) + 0.012).toFixed(6))
    m.elapsed_s = Math.round((Date.now() - Date.parse(a.spawned_at)) / 1000)
    a.progress = Math.min(0.95, (a.progress ?? 0) + 0.02)
    a.activity = ACTIVITIES[beat % ACTIVITIES.length]
    a.heartbeat_at = now()
    broadcast('agent.card.upsert', { agent: a })
  }
}
setInterval(heartbeat, 5000)

function snapshot() {
  return structuredClone(state)
}

function findAgent(id) {
  return state.agents.find((a) => a.id === id)
}

/** the mock's stand-in for `whiteboard.mermaid.parse`: enough to exercise `edit.reject` */
function badMermaid(text) {
  const body = String(text ?? '').trim()
  if (!body) return 'empty diagram'
  if (!/^(flowchart|graph)\s+(TD|TB|LR|RL|BT)\b/.test(body)) {
    return 'mermaid parse error: expected a `flowchart TD` header on line 1'
  }
  return null
}

/** replace (or append) the first fence of a diagrams.md document */
function replaceFence(md, mermaid) {
  const fence = '```mermaid\n' + mermaid.replace(/\s+$/, '') + '\n```'
  if (/```[^\n]*\n[\s\S]*?```/.test(md)) return md.replace(/```[^\n]*\n[\s\S]*?```/, fence)
  return `${md.trimEnd()}\n\n${fence}\n`.trimStart()
}

// ---- diagram proposals (A.2 / B.10) -------------------------------------------------------
const GOOD_PROPOSAL = `flowchart TD
    node-parser["Mermaid parser"]
    node-store["Plan store"]
    node-render["Canvas renderer"]
    node-parser --> node-store
    node-store --> node-render
`
/** no `flowchart TD` header: `badMermaid` refuses it exactly as `validate_diagram` would */
const BAD_PROPOSAL = `nodes:
    node-parser["Mermaid parser"]
    node-parser --> node-store
`

/** the mock's stand-in for `whiteboard.mermaid.parse`: box ids, labels and `-->` edges */
function parseMermaid(text) {
  const nodes = []
  const edges = []
  for (const raw of String(text ?? '').split('\n')) {
    const line = raw.trim()
    if (!line || /^(flowchart|graph)\b/.test(line) || line.startsWith('%%')) continue
    const edge = line.match(/^([\w-]+)\s*--(?:\s*\|([^|]*)\|\s*)?>\s*([\w-]+)/)
    if (edge) {
      edges.push({ src: edge[1], dst: edge[3], label: edge[2]?.trim() || null })
      continue
    }
    const node = line.match(/^([\w-]+)\s*[[({]"?([^\]")}]*)"?[\])}]/)
    if (node) nodes.push({ id: node[1], label: node[2].trim() })
  }
  return { nodes, edges }
}

/** `store.diagram_diff(name, mermaid)`: the proposal against the board as it is right now */
function diagramDiff(name, mermaid) {
  const { nodes, edges } = parseMermaid(mermaid)
  const ids = new Set(nodes.map((n) => n.id))
  const onBoard = state.nodes.filter((n) => (n.type ?? 'hld') === name)
  const boardEdges = state.edges.filter((e) => (e.diagram ?? 'hld') === name)
  const key = (e) => `${e.src}__${e.dst}`
  const have = new Set(boardEdges.map(key))
  return {
    nodes_added: nodes.filter((n) => !findNode(n.id)),
    nodes_removed: onBoard.filter((n) => !ids.has(n.id)).map((n) => n.id),
    edges_added: edges.filter((e) => !have.has(key(e))),
    edges_removed: boardEdges
      .filter((e) => !edges.some((p) => key(p) === key(e)))
      .map((e) => ({ src: e.src, dst: e.dst, label: e.label ?? null })),
  }
}

function diagramRequestPayload(rid, pending) {
  return {
    request_id: rid,
    name: pending.name,
    mermaid: pending.mermaid,
    rationale: pending.rationale,
    ...diagramDiff(pending.name, pending.mermaid),
  }
}

function proposeDiagram(name, mermaid, rationale) {
  const request_id = Math.random().toString(16).slice(2, 10)
  const pending = { name, mermaid, rationale }
  pendingDiagrams.set(request_id, pending)
  const payload = diagramRequestPayload(request_id, pending)
  broadcast('diagram.request', payload)
  appendEvent({ agent_id: 'root', node_id: null, type: 'diagram_proposed', note: `proposed ${name}`, data: { request_id, name } })
  log('proposed diagram', name, request_id)
  return payload
}

/** approval is the only path that writes a board: create the boxes and edges it names */
function writeDiagram(name, mermaid) {
  const { nodes, edges } = parseMermaid(mermaid)
  for (const n of nodes) {
    let node = findNode(n.id)
    if (!node) {
      node = { id: n.id, type: name, title: n.label || n.id, status: 'todo', owner: null, depends_on: [], interfaces: [], body: '' }
      state.nodes.push(node)
    } else if (n.label) node.title = n.label
    broadcast('plan.node.upsert', { node })
  }
  for (const e of edges) {
    let edge = state.edges.find((x) => x.src === e.src && x.dst === e.dst)
    if (!edge) {
      edge = { src: e.src, dst: e.dst, label: e.label, diagram: name }
      state.edges.push(edge)
    }
    const dst = findNode(e.dst)
    if (dst && !dst.depends_on.includes(e.src)) {
      dst.depends_on.push(e.src)
      broadcast('plan.node.upsert', { node: dst })
    }
    broadcast('plan.edge.upsert', { edge })
  }
  return { nodes: nodes.length, edges: edges.length }
}

function findNode(id) {
  return state.nodes.find((n) => n.id === id)
}

function applyOp(op) {
  const diagram = op.diagram ?? 'hld'
  switch (op.op) {
    case 'renamed': {
      const n = findNode(op.id)
      if (!n) return
      n.title = op.label
      broadcast('plan.node.upsert', { node: n })
      appendEvent({ agent_id: 'user', node_id: n.id, type: 'node_changed', note: `renamed to "${op.label}"` })
      break
    }
    case 'node-created': {
      if (findNode(op.id)) return
      const n = { id: op.id, type: 'hld', title: op.label, status: 'todo', owner: null, depends_on: [], interfaces: [], body: '' }
      state.nodes.push(n)
      broadcast('plan.node.upsert', { node: n })
      appendEvent({ agent_id: 'user', node_id: n.id, type: 'node_changed', note: 'created from canvas' })
      break
    }
    case 'status-changed': {
      const n = findNode(op.id)
      if (!n) return
      n.status = op.status
      broadcast('plan.node.upsert', { node: n })
      appendEvent({ agent_id: 'user', node_id: n.id, type: 'node_changed', note: `status → ${op.status}` })
      break
    }
    case 'deleted': {
      state.nodes = state.nodes.filter((n) => n.id !== op.id)
      state.edges = state.edges.filter((e) => e.src !== op.id && e.dst !== op.id)
      for (const n of state.nodes) n.depends_on = n.depends_on.filter((d) => d !== op.id)
      broadcast('plan.node.delete', { id: op.id })
      appendEvent({ agent_id: 'user', node_id: op.id, type: 'node_changed', note: 'deleted' })
      break
    }
    case 'edge-created': {
      if (!state.edges.some((e) => e.src === op.from && e.dst === op.to)) {
        const e = { src: op.from, dst: op.to, label: op.label ?? null, diagram }
        state.edges.push(e)
        const dst = findNode(op.to)
        if (dst && !dst.depends_on.includes(op.from)) dst.depends_on.push(op.from)
        broadcast('plan.edge.upsert', { edge: e })
        if (dst) broadcast('plan.node.upsert', { node: dst })
      }
      break
    }
    case 'edge-deleted': {
      state.edges = state.edges.filter((e) => !(e.src === op.from && e.dst === op.to))
      const dst = findNode(op.to)
      if (dst) dst.depends_on = dst.depends_on.filter((d) => d !== op.from)
      broadcast('plan.edge.delete', { src: op.from, dst: op.to, diagram })
      if (dst) broadcast('plan.node.upsert', { node: dst })
      break
    }
    case 'edge-rerouted': {
      applyOp({ op: 'edge-deleted', from: op.from, to: op.to, diagram })
      applyOp({ op: 'edge-created', from: op.new_from, to: op.new_to, diagram })
      break
    }
    default:
      log('unknown op', op)
  }
}

const RISKY = new Set(['edge-created', 'edge-deleted', 'edge-rerouted', 'deleted'])

function describeOps(ops) {
  return ops
    .map((o) => {
      switch (o.op) {
        case 'edge-created':
          return `${o.to} depends_on +${o.from}`
        case 'edge-deleted':
          return `${o.to} depends_on -${o.from}`
        case 'edge-rerouted':
          return `${o.to} depends_on -${o.from}; ${o.new_to} depends_on +${o.new_from}`
        case 'deleted':
          return `delete ${o.id}`
        default:
          return o.op
      }
    })
    .join('; ')
}

function handle(client, msg) {
  const { type, payload, seq } = msg
  switch (type) {
    case 'client.hello': {
      client.hello = payload
      sendTo(client, 'plan.snapshot', snapshot())
      // the last 50 per thread, ALWAYS and BEFORE the event replay (A.4 `on_hello`)
      const backfill = chatBackfill()
      for (const m of backfill) sendTo(client, 'chat.message', m)
      log(`hello → ${backfill.length} chat.message backfill frame(s)`)
      const since = Number(payload?.lastSeq ?? 0)
      for (const ev of events) {
        if (ev.seq > since) client.ws.send(JSON.stringify({ type: 'event.append', payload: ev, seq: ev.seq, ts: ev.ts, replyTo: null }))
      }
      // A.2.5: every still-pending proposal is re-sent on hello, with its diff recomputed
      for (const [rid, pending] of pendingDiagrams) sendTo(client, 'diagram.request', diagramRequestPayload(rid, pending))
      sendTo(client, 'bridge.status', { ok: true, failures: 0 })
      if (!client.scriptStarted) {
        client.scriptStarted = true
        // live chat in both threads, then the whole backfill AGAIN at 16s (what a reconnect
        // replays): the second burst overlaps every live message, so dedupe-by-id is exercised
        // without pulling the socket
        setTimeout(() => appendEvent({ agent_id: 'root', node_id: null, type: 'chat', note: 'dispatching node-canvas next — anything you want changed first?', data: { from: 'root', to: 'user', thread: 'root' } }), 6000)
        setTimeout(() => appendEvent({ agent_id: 'agent-parser', node_id: 'node-parser', type: 'chat', note: 'fence header parses; moving on to edge labels', data: { from: 'agent-parser', to: 'user', thread: 'agent-parser', reply_to: 'chat-43' } }), 9000)
        // one failing row so the feed's `--wb-blocked` tint is actually exercised (B.8)
        setTimeout(() => appendEvent({ agent_id: 'agent-parser', node_id: 'node-parser', type: 'blocked', note: 'subgraph fences are ambiguous — need a decision' }), 11000)
        setTimeout(() => {
          if (client.ws.readyState !== 1) return
          const again = chatBackfill()
          for (const m of again) sendTo(client, 'chat.message', m)
          log(`replayed ${again.length} chat.message frame(s) — the client must dedupe them all`)
        }, 16000)
        for (const step of scripted) {
          setTimeout(() => {
            if (client.ws.readyState !== 1) return
            const m = step.message
            if (m.type === 'event.append') appendEvent(m.payload)
            else if (m.type === 'plan.node.upsert') {
              const idx = state.nodes.findIndex((n) => n.id === m.payload.node.id)
              if (idx >= 0) state.nodes[idx] = m.payload.node
              else state.nodes.push(m.payload.node)
              broadcast(m.type, m.payload)
            } else if (m.type === 'agent.card.upsert') {
              // the fixture's timestamps are fixed; rebase them so the card's clock stays live
              const agent = rebaseAgent(structuredClone(m.payload.agent))
              const idx = state.agents.findIndex((a) => a.id === agent.id)
              if (idx >= 0) state.agents[idx] = agent
              else state.agents.push(agent)
              broadcast(m.type, { agent })
            } else sendTo(client, m.type, m.payload)
            log('scripted →', m.type)
          }, step.delayMs)
        }
      }
      break
    }
    case 'canvas.edit': {
      const ops = payload?.ops ?? []
      const risky = ops.filter((o) => RISKY.has(o.op))
      const cosmetic = ops.filter((o) => !RISKY.has(o.op))
      for (const op of cosmetic) applyOp(op)
      if (risky.length) {
        const request_id = `r-${Math.random().toString(36).slice(2, 8)}`
        pendingRisky.set(request_id, { ops: risky, forSeq: seq, client })
        const affected = [...new Set(risky.flatMap((o) => [o.from, o.to, o.new_from, o.new_to, o.id].filter(Boolean)))]
        sendTo(client, 'risky_edit.request', {
          request_id,
          summary: describeOps(risky),
          diff: risky.map((o) => JSON.stringify(o)).join('\n'),
          affected,
        })
        appendEvent({ agent_id: 'user', node_id: affected[0] ?? null, type: 'risky_edit', note: describeOps(risky), data: { ops: risky, request_id } })
      } else {
        state.rev += 1
        sendTo(client, 'edit.ack', { forSeq: seq, rev: state.rev }, { replyTo: seq })
      }
      break
    }
    case 'risky_edit.reply': {
      const p = pendingRisky.get(payload.request_id)
      if (!p) return
      pendingRisky.delete(payload.request_id)
      if (payload.approved) {
        for (const op of p.ops) applyOp(op)
        state.rev += 1
        sendTo(client, 'edit.ack', { forSeq: p.forSeq, rev: state.rev }, { replyTo: p.forSeq })
        appendEvent({ agent_id: 'user', node_id: null, type: 'risky_edit_accepted', note: `${describeOps(p.ops)} — "${payload.note ?? ''}"` })
      } else {
        sendTo(client, 'edit.reject', { forSeq: p.forSeq, reason: 'rejected by user', revert: [] }, { replyTo: p.forSeq })
        appendEvent({ agent_id: 'user', node_id: null, type: 'risky_edit_rejected', note: describeOps(p.ops) })
      }
      break
    }
    case 'canvas.layout': {
      const diagram = payload.diagram ?? 'hld'
      const cur = (state.layout[diagram] ??= { version: 1, direction: 'TD', frames: {}, nodes: {}, agents: {}, edges: {} })
      for (const k of ['frames', 'nodes', 'agents', 'edges']) {
        cur[k] = { ...(cur[k] ?? {}), ...(payload.patch?.[k] ?? {}) }
      }
      cur.updatedAt = now()
      broadcast('layout.update', { diagram, layout: cur })
      break
    }
    case 'plan.relayout': {
      const diagram = payload.diagram ?? 'hld'
      const cur = (state.layout[diagram] ??= { version: 1, direction: 'TD', frames: {}, nodes: {}, agents: {}, edges: {} })
      for (const [id, n] of Object.entries(cur.nodes ?? {})) if (!n.pinned) delete cur.nodes[id]
      cur.updatedAt = now()
      broadcast('layout.update', { diagram, layout: cur })
      break
    }
    case 'chat.message': {
      const to = payload.agentId ?? 'root'
      const thread = payload.thread || to
      const mine = appendEvent({
        agent_id: 'user',
        node_id: payload.nodeId ?? null,
        type: 'chat',
        note: payload.text,
        data: { from: 'user', to, thread, reply_to: payload.reply_to ?? null },
      })
      // the answer comes back in the SAME thread and quotes what it answers
      setTimeout(
        () =>
          appendEvent({
            agent_id: to,
            node_id: null,
            type: 'chat',
            note: `ack: "${payload.text.slice(0, 40)}"`,
            data: { from: to, to: 'user', thread, reply_to: `chat-${mine.seq}` },
          }),
        800,
      )
      break
    }
    case 'plan.paste': {
      appendEvent({ agent_id: 'user', node_id: null, type: 'plan_pasted', note: `${payload.text.length} chars pasted` })
      break
    }
    case 'prompt.reply': {
      appendEvent({ agent_id: 'user', node_id: null, type: 'reply', note: String(payload.value), data: { prompt_id: payload.prompt_id } })
      break
    }
    case 'dispatch.reply': {
      if (payload.approved) {
        const agent = { id: 'agent-canvas', assigned_node: 'node-canvas', status: 'working', claude_agent_ref: 'canvas-1', ready_deps: [], spawned_at: now(), notes: 'spawned after approval', plan_md: '', diagrams_md: '' }
        const idx = state.agents.findIndex((a) => a.id === agent.id)
        if (idx >= 0) state.agents[idx] = agent
        else state.agents.push(agent)
        broadcast('agent.card.upsert', { agent })
        appendEvent({ agent_id: 'root', node_id: 'node-canvas', type: 'dispatch_approved', note: payload.note ?? '' })
      } else {
        appendEvent({ agent_id: 'root', node_id: 'node-canvas', type: 'dispatch_rejected', note: payload.note ?? '' })
      }
      break
    }
    case 'agent.plan.edit': {
      const a = findAgent(payload.agent_id)
      if (!a) {
        sendTo(client, 'server.error', { forSeq: seq, code: 'unknown_agent', message: `no agent ${payload.agent_id}` })
        break
      }
      a.plan_md = payload.plan_md ?? ''
      broadcast('agent.card.upsert', { agent: a })
      sendTo(client, 'edit.ack', { forSeq: seq, rev: ++state.rev }, { replyTo: seq })
      appendEvent({ agent_id: 'user', node_id: a.assigned_node, type: 'agent_plan_edited', note: `agent plan edited: ${a.id}`, data: { source: 'canvas', field: 'plan_md' } })
      break
    }
    case 'agent.diagram.edit': {
      const a = findAgent(payload.agent_id)
      if (!a) {
        sendTo(client, 'server.error', { forSeq: seq, code: 'unknown_agent', message: `no agent ${payload.agent_id}` })
        break
      }
      const reason = badMermaid(payload.mermaid)
      if (reason) {
        sendTo(client, 'edit.reject', { forSeq: seq, reason, revert: [] }, { replyTo: seq })
        break
      }
      a.diagrams_md = replaceFence(a.diagrams_md ?? '', payload.mermaid)
      a.diagram = { mermaid: String(payload.mermaid).trim(), direction: 'TD', nodes: [], edges: [], error: null }
      broadcast('agent.card.upsert', { agent: a })
      sendTo(client, 'edit.ack', { forSeq: seq, rev: ++state.rev }, { replyTo: seq })
      appendEvent({ agent_id: 'user', node_id: a.assigned_node, type: 'agent_plan_edited', note: `agent diagram edited: ${a.id}`, data: { source: 'canvas', field: 'diagrams_md' } })
      break
    }
    case 'diagram.reply': {
      const rid = payload.request_id
      const pending = pendingDiagrams.get(rid)
      if (!pending) {
        sendTo(client, 'server.error', { forSeq: seq, code: 'unknown_request', message: `no diagram proposal ${rid}` })
        break
      }
      const text = (payload.mermaid || '').trim() || pending.mermaid
      const edited = Boolean(payload.mermaid) && payload.mermaid.trim() !== pending.mermaid.trim()
      if (!payload.approved) {
        pendingDiagrams.delete(rid)
        sendTo(client, 'edit.ack', { forSeq: seq, rev: state.rev }, { replyTo: seq })
        appendEvent({ agent_id: 'user', node_id: null, type: 'diagram_rejected', note: `diagram rejected: ${pending.name} (req ${rid})`, data: { request_id: rid, note: payload.note ?? '' } })
        break
      }
      const reason = badMermaid(text)
      if (reason) {
        // A.2: the proposal STAYS pending, so the modal reopens with the owner's edit intact
        sendTo(client, 'server.error', { forSeq: seq, code: 'bad_diagram', message: reason })
        break
      }
      pendingDiagrams.delete(rid)
      const wrote = writeDiagram(pending.name, text)
      state.rev += 1
      sendTo(client, 'edit.ack', { forSeq: seq, rev: state.rev }, { replyTo: seq })
      appendEvent({
        agent_id: 'user',
        node_id: null,
        type: 'diagram_approved',
        note: `diagram approved: ${pending.name} (req ${rid}; ${wrote.nodes} nodes, ${wrote.edges} edges)`,
        data: { request_id: rid, name: pending.name, note: payload.note ?? '', edited },
      })
      break
    }
    case 'node.status': {
      applyOp({ op: 'status-changed', id: payload.id, status: payload.status })
      break
    }
    default:
      sendTo(client, 'server.error', { forSeq: seq, code: 'unknown_type', message: `unknown message type ${type}` })
  }
}

wss.on('connection', (ws, req) => {
  const client = { ws, counter: 1_000_000 - 1, hello: null, scriptStarted: false }
  clients.add(client)
  log('client connected from', req.socket.remoteAddress, `(${clients.size} total)`)
  ws.on('message', (data) => {
    let msg
    try {
      msg = JSON.parse(String(data))
    } catch {
      log('bad json from client')
      return
    }
    log('←', msg.type, `seq=${msg.seq}`, JSON.stringify(msg.payload).slice(0, 300))
    try {
      handle(client, msg)
    } catch (err) {
      log('handler error', err)
      sendTo(client, 'server.error', { forSeq: msg.seq, code: 'internal', message: String(err?.message ?? err) })
    }
  })
  ws.on('close', () => {
    clients.delete(client)
    log('client closed', `(${clients.size} total)`)
  })
})

http.listen(PORT, '127.0.0.1', () => {
  log(`mock whiteboard server on http://127.0.0.1:${PORT}  (ws://127.0.0.1:${PORT}/ws)`)
  log(existsSync(DIST) ? `serving ${DIST}` : 'dist/ not built; use `pnpm dev` with WHITEBOARD_PORT=' + PORT)
})
