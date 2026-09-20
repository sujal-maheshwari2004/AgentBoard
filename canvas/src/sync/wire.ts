// Glue: socket ⇄ store ⇄ editor. Mounted once from <Tldraw onMount>; returns a cleanup.
import type { Editor } from 'tldraw'
import { createSocket, socketUrl, type Socket } from '../ws/client'
import { store, type Store } from '../state/store'
import type { ClientPayloads, EditOp, LayoutPatch, NodeStatus, ServerMessage } from '../state/types'
import {
  applyLayout,
  applySnapshot,
  deleteAgent,
  deleteAgentFolder,
  deleteEdge,
  deleteNode,
  ensureBoardFrames,
  folderLayout,
  nodeContext,
  onFolderCollapse,
  refreshAgents,
  relayoutLocal,
  upsertAgentAndFolder,
  upsertEdge,
  upsertNodeFromState,
} from './apply'
import { isBoardName, onBoardReparent, splitLayoutByBoard } from './boards'
import { diagramEditPayload, planEditPayload } from '../shapes/agentFolder'
import { installCollector } from './collect'
import { onFrameCollapse } from './frame'

export interface Wiring {
  socket: Socket
  editor: Editor
  sendOps(ops: EditOp[]): void
  /** `board` defaults to the active board; a patch that spans boards is split per board */
  sendLayout(patch: LayoutPatch, board?: string): void
  setBoard(board: string): void
  setNodeStatus(id: string, status: NodeStatus): void
  /** B.6 folder editors; both return the seq so the sender can match an `edit.reject` */
  editAgentPlan(agentId: string, planMd: string): number
  editAgentDiagram(agentId: string, mermaid: string): number
  /**
   * B.10 owner verdict on a proposed board. Returns the send seq: the modal matches the
   * `edit.ack` / `server.error {code:'bad_diagram'}` that comes back on it.
   */
  replyDiagram(reply: ClientPayloads['diagram.reply']): number
  relayout(): void
  /** re-apply the last known server truth (used after a rejected edit) */
  resync(): void
  dispose(): void
}

let current: Wiring | null = null
export function getWiring(): Wiring | null {
  return current
}

function applyMessage(editor: Editor, s: Store, msg: ServerMessage): void {
  const state = s.getState()
  const nodes = Object.values(state.nodes)
  const edges = Object.values(state.edges)
  const layout = state.layout
  switch (msg.type) {
    case 'plan.snapshot':
      applySnapshot(editor, msg.payload)
      break
    case 'plan.node.upsert':
      upsertNodeFromState(editor, msg.payload.node, nodes, edges, layout)
      refreshAgents(editor, Object.values(state.agents), nodes)
      break
    case 'plan.node.delete':
      deleteNode(editor, msg.payload.id)
      refreshAgents(editor, Object.values(state.agents), nodes)
      break
    case 'plan.edge.upsert':
      upsertEdge(editor, msg.payload.edge)
      break
    case 'plan.edge.delete':
      deleteEdge(editor, msg.payload.src, msg.payload.dst)
      break
    case 'agent.card.upsert': {
      const agent = msg.payload.agent
      const l = layout[state.activeBoard]?.agents?.[agent.id]
      upsertAgentAndFolder(
        editor,
        agent,
        nodeContext(nodes),
        l ? { x: l.x, y: l.y, w: l.w, h: l.h } : undefined,
        folderLayout(layout, agent.id),
      )
      break
    }
    case 'agent.card.delete':
      deleteAgent(editor, msg.payload.id)
      deleteAgentFolder(editor, msg.payload.id)
      break
    case 'layout.update':
      // the active board is what `canvas.layout` / `plan.relayout` name, so it is what we apply
      if (msg.payload.diagram === state.activeBoard) applyLayout(editor, msg.payload.diagram, msg.payload.layout, nodes, edges)
      break
    case 'edit.reject':
      // the server is authoritative: put the canvas back to the last snapshot state
      resyncFromStore(editor, s)
      break
    default:
      break
  }
}

function resyncFromStore(editor: Editor, s: Store): void {
  const state = s.getState()
  if (!state.hasSnapshot) return
  applySnapshot(editor, {
    project: state.project,
    rev: state.rev,
    nodes: Object.values(state.nodes),
    edges: Object.values(state.edges),
    agents: Object.values(state.agents),
    diagrams: state.diagrams,
    layout: state.layout,
  })
}

export function mountWiring(editor: Editor, s: Store = store, url: string = socketUrl()): Wiring {
  current?.dispose()
  ensureBoardFrames(editor)

  const socket = createSocket({
    url,
    onStatus: (st) => s.setSocketStatus(st),
    onMessage: (msg) => {
      s.dispatch(msg)
      try {
        applyMessage(editor, s, msg)
      } catch (err) {
        console.error('[whiteboard] apply failed for', msg.type, err)
        s.note('error', `apply ${msg.type} failed: ${(err as Error).message}`)
      }
    },
  })

  const sendOps = (ops: EditOp[]) => {
    if (!ops.length) return
    const seq = socket.send('canvas.edit', { ops })
    s.note('edit', `sent ${ops.map((o) => o.op).join(', ')} (seq ${seq})`)
  }
  const sendLayout = (patch: LayoutPatch, board?: string) => {
    if (board) {
      socket.send('canvas.layout', { diagram: board, patch })
      return
    }
    // one drag can move nodes on several boards: each board owns its own sidecar
    for (const [b, p] of splitLayoutByBoard(patch, s.getState().activeBoard)) {
      socket.send('canvas.layout', { diagram: b, patch: p })
    }
  }

  const offCollector = installCollector(editor, {
    getDiagram: () => s.getState().activeBoard,
    getEdges: () => Object.values(s.getState().edges),
    onOps: sendOps,
    onLayout: sendLayout,
  })
  const offCollapse = onFrameCollapse((board, patch) => sendLayout(patch, board))
  // B.6: `frames['<agent-id>'].collapsed`, persisted on the board that owns the folder
  const offFolderCollapse = onFolderCollapse((board, patch) => sendLayout(patch, board))
  const offReparent = onBoardReparent((board, patch) => sendLayout(patch, board))

  const wiring: Wiring = {
    socket,
    editor,
    sendOps,
    sendLayout,
    setBoard(board) {
      if (isBoardName(board)) s.setActiveBoard(board)
    },
    setNodeStatus(id, status) {
      socket.send('node.status', { id, status })
    },
    editAgentPlan(agentId, planMd) {
      return socket.send('agent.plan.edit', planEditPayload(agentId, planMd))
    },
    editAgentDiagram(agentId, mermaid) {
      return socket.send('agent.diagram.edit', diagramEditPayload(agentId, mermaid))
    },
    replyDiagram(reply) {
      return socket.send('diagram.reply', reply)
    },
    relayout() {
      const state = s.getState()
      socket.send('plan.relayout', { diagram: state.activeBoard })
      relayoutLocal(editor, state.activeBoard, Object.values(state.nodes), Object.values(state.edges), state.layout[state.activeBoard])
    },
    resync: () => resyncFromStore(editor, s),
    dispose() {
      offCollector()
      offCollapse()
      offFolderCollapse()
      offReparent()
      socket.close()
      if (current === wiring) current = null
    },
  }
  current = wiring
  return wiring
}
