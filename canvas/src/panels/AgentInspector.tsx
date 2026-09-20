// Agent inspector island (plan B.8/B.11): the "watch one agent work" view that v1 had nowhere.
// Opens on agent-card selection (`store.inspectorAgentId`), sits in the right column above the
// chat island, and shows — for one agent —
//
//   status ring + name + status + elapsed        (the shared `nowMs` clock, frozen at finished_at)
//   ▸ activity                                   (greyed with `· stalled?` after 90s of silence)
//   ▬▬▬▬░░░░ 62%                                 progress rail in the status colour
//   14 tools · 6 files · ∑ 41.2k tok · $0.38     `formatMetricsLine`
//   the agent's own slice of the retained raw events
//   plan.md and diagrams.md editors               (`agent.plan.edit` / `agent.diagram.edit`)
//   a link into its chat thread
//
// The editors reuse the B.S3 payload helpers through `Wiring.editAgentPlan/editAgentDiagram`,
// and an inline error is matched on `edit.reject.forSeq === sentSeq` so one agent's parse error
// can never surface under another's box.
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { store, useStore } from '../state/store'
import type { AgentCard, PlanEvent } from '../state/types'
import { agentElapsed, agentStalled, formatElapsed, formatMetricsLine } from '../state/format'
import { agentCardState, statusColor } from '../shapes/agentCard'
import { EDIT_DEBOUNCE_MS, folderMermaid } from '../shapes/agentFolder'
import { revealAgentFolder } from '../sync/apply'
import { getWiring } from '../sync/wire'

/** how much of the agent's feed the island shows before it scrolls */
const FEED_LIMIT = 12

/** events that belong to one agent: its own, ones it was notified of, and ones on its node */
export function agentEvents(events: PlanEvent[], agent: Pick<AgentCard, 'id' | 'assigned_node'>): PlanEvent[] {
  const node = agent.assigned_node
  return events.filter(
    (ev) => ev.agent_id === agent.id || (!!node && ev.node_id === node) || (ev.notified?.includes(agent.id) ?? false),
  )
}

export function AgentInspector() {
  const agentId = useStore((s) => s.inspectorAgentId)
  const agent = useStore((s) => (s.inspectorAgentId ? s.agents[s.inspectorAgentId] : undefined))
  const chatOpen = useStore((s) => s.chatOpen)
  const nowMs = useStore((s) => s.nowMs)
  const events = useStore((s) => s.events)
  const unread = useStore((s) => (s.inspectorAgentId ? (s.threadUnread[s.inspectorAgentId] ?? 0) : 0))

  const feed = useMemo(() => (agent ? agentEvents(events, agent).slice(-FEED_LIMIT).reverse() : []), [events, agent])

  if (!agentId || !agent) return null

  const state = agentCardState(agent.status)
  const elapsed = agentElapsed(agent, nowMs)
  const stalled = agentStalled(agent, nowMs)
  const progress = typeof agent.progress === 'number' ? Math.min(1, Math.max(0, agent.progress)) : -1
  const activity = agent.activity || agent.notes || (agent.assigned_node ? `on ${agent.assigned_node}` : 'unassigned')
  // the chat island is 420px tall expanded and a 44px pill collapsed; the inspector stacks on it
  const bottom = chatOpen ? 436 : 60

  return (
    <div className="wb-panel wb-inspector" style={{ bottom }} data-testid="agent-inspector">
      <div className="wb-insp-head">
        <span
          className={`wb-insp-ring ${state.cls}`}
          style={{ ['--wb-ring' as string]: statusColor(agent.status) }}
          title={state.label}
          aria-label={state.label}
          role="img"
        />
        <span className="wb-insp-name">{agent.id}</span>
        <span className="wb-insp-status">{state.label}</span>
        {elapsed !== null && <span className="wb-insp-elapsed">⏱ {formatElapsed(elapsed)}</span>}
        <button
          type="button"
          className="wb-chat-collapse"
          title="Close the inspector"
          aria-label="Close the inspector"
          onClick={() => store.inspectAgent(null)}
        >
          ✕
        </button>
      </div>

      <div className={`wb-insp-activity${stalled ? ' stalled' : ''}`} title={activity}>
        ▸ {activity}
        {stalled ? ' · stalled?' : ''}
      </div>

      <div className="wb-insp-progress">
        <div
          className={`wb-insp-rail ${state.cls}`}
          role="progressbar"
          aria-valuemin={0}
          aria-valuemax={100}
          aria-valuenow={progress >= 0 ? Math.round(progress * 100) : undefined}
        >
          <span className="wb-insp-rail-fill" style={{ width: `${Math.max(0, progress) * 100}%` }} />
        </div>
        <span className="wb-insp-pct">{progress >= 0 ? `${Math.round(progress * 100)}%` : '—'}</span>
      </div>

      <div className="wb-insp-metrics">{formatMetricsLine(agent.metrics)}</div>

      <div className="wb-insp-actions">
        <button type="button" className="wb-btn" onClick={() => store.openThread(agent.id)}>
          Chat thread{unread > 0 ? ` (${unread})` : ''}
        </button>
        <button
          type="button"
          className="wb-btn"
          onClick={() => {
            const editor = getWiring()?.editor
            if (editor) revealAgentFolder(editor, agent.id)
          }}
        >
          Open folder
        </button>
      </div>

      <section className="wb-insp-section">
        <header className="wb-insp-label">activity feed</header>
        <ul className="wb-insp-feed">
          {feed.length === 0 && <li className="wb-insp-empty">no events for this agent yet</li>}
          {feed.map((ev) => (
            <li key={`${ev.seq}-${ev.ts}`} className={isErrorEvent(ev) ? 'err' : undefined} title={ev.ts}>
              <span className="seq">#{ev.seq}</span>
              <span className="k">{ev.type}</span>
              {ev.note}
            </li>
          ))}
        </ul>
      </section>

      <PlanEditor agentId={agent.id} planMd={agent.plan_md ?? ''} />
      <DiagramEditor agentId={agent.id} mermaid={folderMermaid(agent)} serverError={agent.diagram?.error ?? ''} />
    </div>
  )
}

/** the failing path is lit up (B.1 item 9): blocked / rejected rows take `--wb-blocked` */
export function isErrorEvent(ev: PlanEvent): boolean {
  return (
    ev.type === 'blocked' ||
    ev.type.endsWith('_rejected') ||
    ev.type === 'error' ||
    typeof (ev.data as { error?: unknown } | undefined)?.error === 'string'
  )
}

/** keeps the caret while the server echoes our own text back through `agent.card.upsert` */
function useServerText(incoming: string): [string, (v: string) => void] {
  const [text, setText] = useState(incoming)
  const last = useRef(incoming)
  useEffect(() => {
    if (incoming !== last.current) {
      last.current = incoming
      setText(incoming)
    }
  }, [incoming])
  return [text, setText]
}

function PlanEditor({ agentId, planMd }: { agentId: string; planMd: string }) {
  const [text, setText] = useServerText(planMd)
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null)
  useEffect(() => () => { if (timer.current) clearTimeout(timer.current) }, [])
  const onChange = useCallback(
    (v: string) => {
      setText(v)
      if (timer.current) clearTimeout(timer.current)
      timer.current = setTimeout(() => getWiring()?.editAgentPlan(agentId, v), EDIT_DEBOUNCE_MS)
    },
    [agentId, setText],
  )
  return (
    <section className="wb-insp-section">
      <header className="wb-insp-label">plan.md</header>
      <textarea
        className="wb-insp-text"
        spellCheck={false}
        value={text}
        aria-label={`plan for ${agentId}`}
        onChange={(e) => onChange(e.target.value)}
        onKeyDown={(e) => e.stopPropagation()}
      />
    </section>
  )
}

function DiagramEditor({ agentId, mermaid, serverError }: { agentId: string; mermaid: string; serverError: string }) {
  const [text, setText] = useServerText(mermaid)
  const [sentSeq, setSentSeq] = useState<number | null>(null)
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const reject = useStore((s) => s.lastReject)
  useEffect(() => () => { if (timer.current) clearTimeout(timer.current) }, [])

  const onChange = useCallback(
    (v: string) => {
      setText(v)
      if (timer.current) clearTimeout(timer.current)
      timer.current = setTimeout(() => {
        const seq = getWiring()?.editAgentDiagram(agentId, v)
        setSentSeq(typeof seq === 'number' ? seq : null)
      }, EDIT_DEBOUNCE_MS)
    },
    [agentId, setText],
  )

  // the seq we sent is what tells this box the rejection is ours and not another editor's
  const rejected = sentSeq !== null && reject?.forSeq === sentSeq ? reject.reason : ''
  const error = rejected || serverError

  return (
    <section className="wb-insp-section">
      <header className="wb-insp-label">diagrams.md</header>
      <textarea
        className="wb-insp-text wb-insp-mono"
        spellCheck={false}
        value={text}
        aria-label={`diagram for ${agentId}`}
        onChange={(e) => onChange(e.target.value)}
        onKeyDown={(e) => e.stopPropagation()}
      />
      {error && (
        <div className="wb-insp-error" role="alert">
          {error}
        </div>
      )}
    </section>
  )
}
