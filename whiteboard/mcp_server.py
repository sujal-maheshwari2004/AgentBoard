"""The ``whiteboard`` MCP tool surface (CONTRACTS §8).

:func:`build_mcp_server` wires an :class:`MCPServer` named ``whiteboard`` over a
:class:`PlanStore`, an :class:`EventLog` and the server's ``Bus``. Every tool
returns a JSON-serializable dict; every anticipated failure raises
:class:`WhiteboardToolError`, which is a ``ValueError`` (per the contract) *and*
an ``mcp`` ``ToolError`` so the message reaches the calling model instead of the
SDK's generic "Error executing tool" text.

All tools are ``async def`` so their bodies run on the server's event loop (the
SDK would otherwise run sync tools in a worker thread, racing the watcher's
``apply_file_change`` on the shared, non-thread-safe ``PlanStore``).
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Protocol

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from whiteboard.events.log import EventLog
from whiteboard.files.frontmatter import split_frontmatter
from whiteboard.liaison import fetch_peer_node, fetch_skeleton
from whiteboard.plan.graph import Graph
from whiteboard.plan.jobspec import render_job_spec
from whiteboard.plan.model import (
    AgentCard,
    Node,
    Skeleton,
    validate_agent_id,
    validate_node_id,
)
from whiteboard.plan.store import PlanStore

__all__ = [
    "EVENT_TYPES",
    "MAX_RESULT_CHARS",
    "WAIT_CAP_S",
    "Bus",
    "WhiteboardToolError",
    "build_mcp_server",
    "read_only_skeleton",
]

_logger = logging.getLogger(__name__)

EVENT_TYPES: tuple[str, ...] = (
    "done", "blocked", "needs_input", "reply", "chat",
    "dispatch_proposed", "dispatch_approved", "dispatch_rejected", "spawned",
    "risky_edit", "risky_edit_accepted", "risky_edit_rejected",
    "plan_pasted", "node_changed", "agent_changed", "info",
)
ROOT_IDS: tuple[str, ...] = ("root", "user", "server")

MAX_RESULT_CHARS = 400_000
TRUNCATED_FIELD_CHARS = 2_000
TRUNCATABLE_FIELDS: tuple[str, ...] = ("body", "plan_md", "diagrams_md", "mermaid", "text", "notes")
WAIT_CAP_S = 25.0

INSTRUCTIONS = """\
whiteboard: a per-project plan/task graph that lives as markdown under <project>/.whiteboard/.

Files are the truth. Node files (.whiteboard/plan/nodes/<node-id>.md, YAML frontmatter + body),
diagram files (.whiteboard/plan/<name>.md with one ```mermaid flowchart) and agent folders
(.whiteboard/agents/<agent-id>/{card,plan,diagrams}.md) are the state; this server reads and
writes them and mirrors every change to the canvas. PLAN.md is generated - never hand-edit it.

Ids: node ids match ^node-[a-z0-9][a-z0-9-]*$, agent ids match ^agent-[a-z0-9][a-z0-9-]*$.
An edge A --> B in a diagram means B depends on A. Frontmatter depends_on wins over diagrams.

Event bookkeeping: append_event(type="done", agent_id=..., node_id=...) marks the node done,
records the node in ready_deps of every agent whose node depends on it, lists those agents in
the event's `notified`, and tells the root session which nodes are now dispatchable.
type="blocked" marks the node and agent blocked; type="needs_input" also opens a prompt.

ask_user is non-blocking: it returns a prompt_id immediately; the human answers on the canvas,
the reply is appended as a `reply` event, relayed to the root session, and readable with
get_reply(prompt_id). Poll get_reply or wait_for_events instead of blocking.

Dispatch flow (root session): read_plan / get_dispatchable -> propose_dispatch(node_id,
agent_id, job_spec_md) writes .whiteboard/agents/<agent-id>/plan.md and asks the human on the
canvas -> the approval arrives in the root session as a push line and a `dispatch_approved`
event -> the root spawns the subagent with that job spec and records the handle with
upsert_agent(claude_agent_ref=...). Subagents call set_agent_status("working") first and
append_event(type="done"|"blocked"|"needs_input") last.
"""


class Bus(Protocol):
    """What the tools need from the server (CONTRACTS §7)."""

    def publish(self, type: str, payload: dict, *, seq: int | None = None) -> None: ...

    def push_root(self, lines: list[str]) -> None: ...

    def bridge_status(self) -> dict: ...


class WhiteboardToolError(ToolError, ValueError):
    """A tool failure with a message the caller is meant to read."""


def _json(model: Any) -> Any:
    if hasattr(model, "model_dump"):
        return model.model_dump(mode="json")
    return model


def _truncate(value: Any, *, state: dict[str, bool]) -> Any:
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            if k in TRUNCATABLE_FIELDS and isinstance(v, str) and len(v) > TRUNCATED_FIELD_CHARS:
                out[k] = v[:TRUNCATED_FIELD_CHARS] + "\n...[truncated]"
                state["hit"] = True
            else:
                out[k] = _truncate(v, state=state)
        return out
    if isinstance(value, list):
        return [_truncate(v, state=state) for v in value]
    return value


def guard_result(result: dict) -> dict:
    """Shrink ``result`` when its JSON exceeds :data:`MAX_RESULT_CHARS`: long
    text fields (body, plan_md, ...) are cut and ``truncated: true`` is added."""
    try:
        size = len(json.dumps(result, default=str))
    except (TypeError, ValueError):
        return result
    if size <= MAX_RESULT_CHARS:
        return result
    state = {"hit": False}
    out = _truncate(result, state=state)
    out["truncated"] = True
    return out


def read_only_skeleton(project_path: Path) -> Skeleton:
    """Skeleton of a project read straight from its node files, writing nothing."""
    root = Path(project_path).expanduser().resolve()
    nodes_dir = root / ".whiteboard" / "plan" / "nodes"
    nodes: list[dict] = []
    if nodes_dir.is_dir():
        for path in sorted(nodes_dir.glob("*.md")):
            if path.name.startswith("."):
                continue
            try:
                meta, body = split_frontmatter(path.read_text(encoding="utf-8"))
                node = Node.from_frontmatter(meta, body, path=str(path.relative_to(root)))
            except (OSError, ValueError) as exc:
                _logger.warning("peer %s: skipping %s: %s", root, path.name, exc)
                continue
            nodes.append(
                {"id": node.id, "title": node.title, "type": node.type, "status": node.status,
                 "depends_on": list(node.depends_on)}
            )
    return Skeleton(project=root.name, nodes=nodes)


def _wrap_errors(fn: Callable[..., Awaitable[dict]]) -> Callable[..., Awaitable[dict]]:
    """Run ``fn``, size-guard its result and turn ValueError/KeyError into
    :class:`WhiteboardToolError` so the message reaches the caller."""

    @functools.wraps(fn)
    async def inner(*args: Any, **kwargs: Any) -> dict:
        try:
            return guard_result(await fn(*args, **kwargs))
        except WhiteboardToolError:
            raise
        except (ValueError, KeyError) as exc:
            msg = exc.args[0] if isinstance(exc, KeyError) and exc.args else str(exc)
            raise WhiteboardToolError(str(msg)) from exc

    return inner


def build_mcp_server(store: PlanStore, log: EventLog, bus: Bus, root: Path) -> MCPServer:
    """Create the ``whiteboard`` MCPServer over ``store``/``log``/``bus`` for project ``root``.

    Mount with ``server.streamable_http_app(streamable_http_path="/mcp", stateless_http=True,
    json_response=True)`` and run ``server.session_manager.run()`` in the app lifespan.
    """
    root = Path(root).resolve()
    server = MCPServer("whiteboard", instructions=INSTRUCTIONS, version="0.1.0")

    # -- helpers --------------------------------------------------------------
    def drain() -> list[dict]:
        """Take (and clear) the store's pending broadcast messages."""
        msgs = list(store.last_messages)
        store.last_messages = []
        return msgs

    def publish_store() -> list[dict]:
        msgs = drain()
        for m in msgs:
            bus.publish(m["type"], m["payload"])
        return msgs

    def record(
        agent_id: str, node_id: str | None, type_: str, note: str = "",
        *, notified: list[str] | None = None, data: dict | None = None,
    ):
        ev = log.append(agent_id=agent_id, node_id=node_id, type=type_, note=note, notified=notified, data=data)
        bus.publish("event.append", ev.model_dump(), seq=ev.seq)
        return ev

    def need_node(node_id: str) -> Node:
        validate_node_id(node_id)
        node = store.get_node(node_id)
        if node is None:
            raise WhiteboardToolError(f"unknown node {node_id!r}")
        return node

    def need_agent(agent_id: str) -> AgentCard:
        validate_agent_id(agent_id)
        card = store.get_agent(agent_id)
        if card is None:
            raise WhiteboardToolError(f"unknown agent {agent_id!r}")
        return card

    def check_event_agent(agent_id: str) -> str:
        if agent_id in ROOT_IDS:
            return agent_id
        return validate_agent_id(agent_id)

    def events_payload(since_seq: int, limit: int) -> dict:
        events = log.read_since(int(since_seq), int(limit))
        return {"events": [e.model_dump() for e in events], "latest_seq": log.latest_seq}

    def server_port() -> int | None:
        try:
            data = json.loads((root / ".whiteboard" / "server.json").read_text(encoding="utf-8"))
            port = data.get("port")
            return int(port) if port is not None else None
        except (OSError, ValueError, AttributeError):
            return None

    def is_local(project_path: str | None) -> bool:
        if project_path is None or str(project_path).strip() == "":
            return True
        try:
            return Path(project_path).expanduser().resolve() == root
        except OSError:
            return False

    def prompt_line(prompt_id: str, agent_id: str, node_id: str | None, question: str) -> str:
        where = f" on {node_id}" if node_id else ""
        return f"needs_input (prompt {prompt_id}, {agent_id}{where}): {json.dumps(question)}"

    def open_prompt(agent_id: str, question: str, node_id: str | None, kind: str, choices: list[str]):
        """Create a prompt (this appends the needs_input event), broadcast it and
        push a root line. Returns the needs_input Event."""
        if not question or not question.strip():
            raise WhiteboardToolError("question must not be empty")
        prompt_id = log.create_prompt(agent_id, question, node_id=node_id, kind=kind, choices=choices)
        ev = next(
            e for e in reversed(log.read_since(log.latest_seq - 1))
            if e.type == "needs_input" and e.data.get("prompt_id") == prompt_id
        )
        bus.publish("event.append", ev.model_dump(), seq=ev.seq)
        bus.publish(
            "needs_input",
            {"prompt_id": prompt_id, "agent_id": agent_id, "node_id": node_id,
             "question": question, "kind": kind, "choices": choices},
        )
        bus.push_root([prompt_line(prompt_id, agent_id, node_id, question)])
        return ev

    def tool(fn: Callable[..., Awaitable[dict]]) -> Callable[..., Awaitable[dict]]:
        wrapped = _wrap_errors(fn)
        server.tool(name=fn.__name__)(wrapped)
        return wrapped

    # -- read tools -----------------------------------------------------------
    @tool
    async def status() -> dict:
        """Project name, server port, plan rev, node/agent counts, latest event seq and bridge status."""
        return {
            "project": store.project,
            "port": server_port(),
            "rev": store.rev,
            "nodes": len(store.nodes),
            "agents": len(store.agents),
            "latest_seq": log.latest_seq,
            "bridge": bus.bridge_status(),
        }

    @tool
    async def read_plan() -> dict:
        """The full plan snapshot: nodes, edges, agents and diagrams (layout omitted)."""
        snap = store.snapshot().model_dump(mode="json")
        snap.pop("layout", None)
        return snap

    @tool
    async def read_node(id: str) -> dict:
        """One node (frontmatter fields + body) by id."""
        return _json(need_node(id))

    @tool
    async def read_collaboration() -> dict:
        """The text of .whiteboard/COLLABORATION.md (read it before acting)."""
        return {"text": store.collaboration_text()}

    @tool
    async def read_plan_md() -> dict:
        """The generated .whiteboard/PLAN.md text."""
        return {"text": store.plan_md_text()}

    # -- node / diagram mutations ---------------------------------------------
    @tool
    async def upsert_node(
        id: str,
        title: str,
        type: str = "hld",
        status: str | None = None,
        depends_on: list[str] | None = None,
        interfaces: list[str | dict] | None = None,
        body: str | None = None,
        diagram: str | None = None,
    ) -> dict:
        """Create or update a node file; depends_on is synced into the diagrams and PLAN.md.

        `diagram` is an extra board to draw the node on; every node is always
        on its type's board (hld | lld | er).
        """
        validate_node_id(id)
        existed = store.get_node(id) is not None
        node = store.upsert_node(
            id, title=title, type=type, status=status, depends_on=depends_on,
            interfaces=interfaces, body=body, diagram=diagram,
        )
        publish_store()
        record("root", id, "node_changed", f"{'updated' if existed else 'created'} {id} ({node.title})")
        return _json(node)

    @tool
    async def set_node_status(id: str, status: str, owner: str | None = None) -> dict:
        """Set a node's status (todo | in_progress | blocked | done) and optionally its owner agent."""
        need_node(id)
        node = store.set_node_status(id, status, owner)
        publish_store()
        extra = f", owner {owner}" if owner else ""
        record("root", id, "node_changed", f"{id} status -> {status}{extra}")
        return _json(node)

    @tool
    async def delete_node(id: str) -> dict:
        """Delete a node file and drop it from diagrams and other nodes' depends_on."""
        need_node(id)
        store.delete_node(id)
        publish_store()
        record("root", id, "node_changed", f"deleted {id}")
        return {"deleted": id}

    @tool
    async def write_diagram(name: str, mermaid: str) -> dict:
        """Replace the mermaid flowchart of plan/<name>.md; boxes become nodes, edges become depends_on."""
        before = set(store.nodes)
        diagram = store.write_diagram(name, mermaid)
        publish_store()
        created = [nid for nid in store.nodes if nid not in before]
        note = f"diagram {name} written ({len(diagram.edges)} edges)"
        if created:
            note += "; created " + ", ".join(created)
        record("root", None, "node_changed", note, data={"diagram": name, "created": created})
        return _json(diagram)

    # -- agents ---------------------------------------------------------------
    @tool
    async def upsert_agent(
        agent_id: str,
        assigned_node: str,
        status: str = "idle",
        plan_md: str | None = None,
        diagrams_md: str | None = None,
        claude_agent_ref: str | None = None,
        notes: str | None = None,
    ) -> dict:
        """Create or update .whiteboard/agents/<agent_id>/ (card, job spec, diagrams)."""
        validate_agent_id(agent_id)
        existed = store.get_agent(agent_id) is not None
        card = store.upsert_agent(
            agent_id, assigned_node, status=status, plan_md=plan_md, diagrams_md=diagrams_md,
            claude_agent_ref=claude_agent_ref, notes=notes,
        )
        publish_store()
        record(
            "root", card.assigned_node, "agent_changed",
            f"{'updated' if existed else 'created'} {agent_id} on {card.assigned_node} ({card.status})",
        )
        return _json(card)

    @tool
    async def set_agent_status(agent_id: str, status: str, note: str = "") -> dict:
        """Set an agent's status (idle | working | blocked | done); appends an agent_changed event."""
        need_agent(agent_id)
        card = store.set_agent_status(agent_id, status)
        publish_store()
        text = f"{agent_id} status -> {status}" + (f": {note}" if note else "")
        record(agent_id, card.assigned_node, "agent_changed", text)
        return _json(card)

    # -- events ---------------------------------------------------------------
    @tool
    async def append_event(
        agent_id: str,
        node_id: str | None,
        type: str,
        note: str = "",
        data: dict | None = None,
    ) -> dict:
        """Append an event. type=done marks the node done and notifies dependents' agents;
        blocked marks node+agent blocked; needs_input opens a prompt for the human."""
        if type not in EVENT_TYPES:
            raise WhiteboardToolError(f"invalid event type {type!r}; expected one of {EVENT_TYPES}")
        check_event_agent(agent_id)
        if node_id is not None:
            need_node(node_id)
        data = dict(data or {})
        notified: list[str] = []
        lines: list[str] = []

        if type == "done" and node_id:
            before = set(Graph(store.nodes).dispatchable())
            store.set_node_status(node_id, "done")
            publish_store()
            card = store.get_agent(agent_id)
            if card is not None and card.assigned_node == node_id and card.status != "done":
                store.set_agent_status(agent_id, "done")
                publish_store()
            store.mark_ready_deps(node_id)
            publish_store()  # agent.card.upsert for every changed card
            dependents = Graph(store.nodes).dependents(node_id)
            for c in store.agents.values():
                if c.assigned_node in dependents and c.id not in notified:
                    notified.append(c.id)
            for dep_id in dependents:
                owner = store.nodes[dep_id].owner
                if owner and owner not in notified:
                    notified.append(owner)
            now = [nid for nid in Graph(store.nodes).dispatchable() if nid not in before]
            data.setdefault("dispatchable", now)
            lines.append(
                f"{agent_id} done on {node_id}; notified {', '.join(notified) or 'nobody'}; "
                f"now dispatchable: {', '.join(now) or 'nothing new'}"
            )
        elif type == "blocked":
            if store.get_agent(agent_id) is not None:
                store.set_agent_status(agent_id, "blocked")
                publish_store()
            if node_id:
                store.set_node_status(node_id, "blocked")
                publish_store()
            where = f" on {node_id}" if node_id else ""
            lines.append(f"{agent_id} blocked{where}: {note or '(no note)'}")
        elif type == "needs_input":
            kind = str(data.get("kind") or "text")
            choices = [str(c) for c in (data.get("choices") or [])]
            return open_prompt(agent_id, note, node_id, kind, choices).model_dump()
        else:
            where = f" on {node_id}" if node_id else ""
            to = data.get("to")
            label = f"{type} (to: {to})" if to else type
            lines.append(f"{label} from {agent_id}{where}: {note or '(no note)'}")

        ev = record(agent_id, node_id, type, note, notified=notified, data=data)
        bus.push_root(lines)
        return ev.model_dump()

    @tool
    async def ask_user(
        agent_id: str,
        question: str,
        choices: list[str] | None = None,
        node_id: str | None = None,
        kind: str = "text",
    ) -> dict:
        """Ask the human a question on the canvas (non-blocking); poll get_reply(prompt_id)."""
        check_event_agent(agent_id)
        if node_id is not None:
            need_node(node_id)
        ev = open_prompt(agent_id, question, node_id, kind, [str(c) for c in (choices or [])])
        return {"prompt_id": ev.data["prompt_id"]}

    @tool
    async def get_reply(prompt_id: str) -> dict:
        """Whether the human answered prompt_id yet, and the value if so."""
        prompt = log.get_prompt(prompt_id)
        if prompt is None:
            raise WhiteboardToolError(f"unknown prompt_id {prompt_id!r}")
        out: dict[str, Any] = {"answered": bool(prompt.get("answered"))}
        if out["answered"]:
            out["value"] = prompt.get("value")
        return out

    # -- dispatch -------------------------------------------------------------
    @tool
    async def get_dispatchable() -> dict:
        """Nodes with status todo, no owner and every dependency done."""
        ids = Graph(store.nodes).dispatchable()
        return {"nodes": [_json(store.nodes[i]) for i in ids]}

    @tool
    async def propose_dispatch(node_id: str, agent_id: str, job_spec_md: str | None = None) -> dict:
        """Write the agent folder (status idle) with the job spec and ask the human to approve
        spawning it. An empty job_spec_md is generated from the node and its dependencies."""
        node = need_node(node_id)
        validate_agent_id(agent_id)
        spec = job_spec_md if job_spec_md and job_spec_md.strip() else None
        if spec is None:
            deps = [store.nodes[d] for d in node.depends_on if d in store.nodes]
            dependents = [store.nodes[d] for d in Graph(store.nodes).dependents(node_id)]
            spec = render_job_spec(node, deps, dependents, root)
        store.upsert_agent(agent_id, node_id, status="idle", plan_md=spec)
        publish_store()  # agent.card.upsert
        request_id = uuid.uuid4().hex[:8]
        record(
            "root", node_id, "dispatch_proposed", f"propose {node_id} -> {agent_id}",
            data={"request_id": request_id, "node_id": node_id, "agent_id": agent_id},
        )
        bus.publish(
            "dispatch.request",
            {"request_id": request_id, "node_id": node_id, "agent_id": agent_id, "job_spec_md": spec},
        )
        return {"request_id": request_id, "job_spec_md": spec}

    # -- event reads ----------------------------------------------------------
    @tool
    async def get_events(since_seq: int = 0, limit: int = 200) -> dict:
        """Events with seq > since_seq (oldest first, at most limit) and the latest seq."""
        if since_seq < 0 or limit < 0:
            raise WhiteboardToolError("since_seq and limit must be >= 0")
        return events_payload(since_seq, limit)

    @tool
    async def wait_for_events(since_seq: int, timeout_s: float = 20) -> dict:
        """Like get_events but waits (up to 25 s) for an event newer than since_seq."""
        if since_seq < 0:
            raise WhiteboardToolError("since_seq must be >= 0")
        if log.latest_seq > since_seq:
            return events_payload(since_seq, 200)
        timeout = max(0.0, min(float(timeout_s), WAIT_CAP_S))
        q = log.subscribe()
        try:
            await asyncio.wait_for(q.get(), timeout)
        except asyncio.TimeoutError:
            pass
        finally:
            log.unsubscribe(q)
        return events_payload(since_seq, 200)

    # -- cross-project --------------------------------------------------------
    @tool
    async def get_skeleton(project_path: str | None = None) -> dict:
        """Compact node list of this project, or of another project (its live server, else its files)."""
        if is_local(project_path):
            return _json(store.skeleton())
        assert project_path is not None
        skel = await fetch_skeleton(project_path, file_loader=read_only_skeleton)
        return _json(skel)

    @tool
    async def read_peer_node(project_path: str, node_id: str) -> dict:
        """One node of another project (its live server, else its files)."""
        validate_node_id(node_id)
        if is_local(project_path):
            return _json(need_node(node_id))
        try:
            return await fetch_peer_node(project_path, node_id, file_loader=read_only_skeleton)
        except KeyError as exc:
            raise WhiteboardToolError(str(exc.args[0] if exc.args else exc)) from exc

    # -- layout ---------------------------------------------------------------
    @tool
    async def save_layout(diagram: str, patch: dict) -> dict:
        """Merge a layout patch into plan/<diagram>.layout.json (cosmetic; the canvas does this itself)."""
        layout = store.save_layout(diagram, patch or {})
        bus.publish("layout.update", {"diagram": diagram, "layout": layout})
        return {"ok": True}

    return server
