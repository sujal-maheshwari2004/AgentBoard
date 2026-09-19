"""Websocket message dispatcher (CONTRACTS §6 client -> server, §9 root push lines).

Every client message except ``canvas.layout``/``plan.relayout`` is appended to
the event log (the event pump broadcasts it as ``event.append``) and, when it
is semantic, pushed to the root session as one bullet line.
"""

from __future__ import annotations

import copy
import json
import logging
import re
from typing import Any

from starlette.websockets import WebSocket, WebSocketDisconnect

from whiteboard.files.frontmatter import split_frontmatter
from whiteboard.mermaid import MermaidError, extract_mermaid_blocks, parse
from whiteboard.plan import layout as layout_mod
from whiteboard.plan.model import Node, slugify
from whiteboard.plan.risk import op_kind
from whiteboard.server import protocol as P
from whiteboard.server.bus import PendingEdit, ServerContext
from whiteboard.server.hub import Connection, envelope

__all__ = ["serve_websocket", "dispatch", "origin_allowed", "describe_ops", "ingest_paste"]

log = logging.getLogger(__name__)

ORIGIN_RE = re.compile(r"^https?://(localhost|127\.0\.0\.1|\[::1\])(:\d+)?$", re.IGNORECASE)
ALL_EVENTS_LIMIT = 1_000_000


def origin_allowed(origin: str | None) -> bool:
    """Missing origin (non-browser clients) or a loopback origin on any port."""
    if not origin:
        return True
    return ORIGIN_RE.match(origin.strip()) is not None


def _q(text: Any, limit: int = 200) -> str:
    """Quote free text for a push line, one line, truncated."""
    s = " ".join(str(text if text is not None else "").split())
    if len(s) > limit:
        s = s[: limit - 1] + "…"
    return json.dumps(s, ensure_ascii=False)


def _flat(text: str, sep: str = "; ") -> str:
    return sep.join(line.strip() for line in str(text or "").splitlines() if line.strip())


def describe_ops(ops: list[dict]) -> list[str]:
    """One human line per canvas op (for the event note and the push line)."""
    out: list[str] = []
    for op in ops:
        if not isinstance(op, dict):
            continue
        kind = op_kind(op)
        if kind == "renamed":
            out.append(f"{op.get('id')} renamed to {_q(op.get('label'))}")
        elif kind == "node-created":
            label = str(op.get("label") or "").strip()
            nid = str(op.get("id") or "").strip() or f"node-{slugify(label)}"
            out.append(f"{nid} created ({_q(label or nid)}) in {op.get('diagram') or 'hld'}")
        elif kind == "status-changed":
            out.append(f"{op.get('id')} status → {op.get('status')}")
        elif kind == "deleted":
            out.append(f"{op.get('id')} deleted")
        elif kind == "edge-created":
            out.append(f"{op.get('to')} now depends on {op.get('from')}")
        elif kind == "edge-deleted":
            out.append(f"{op.get('to')} no longer depends on {op.get('from')}")
        elif kind == "edge-rerouted":
            out.append(f"edge {op.get('from')} → {op.get('to')} rerouted to {op.get('new_from')} → {op.get('new_to')}")
        else:
            out.append(f"{kind or 'unknown op'}")
    return out


# --------------------------------------------------------------------------- send helpers
def _send(ctx: ServerContext, conn: Connection, type: str, payload: Any, *, reply_to: int | None = None, seq: int | None = None) -> None:
    ctx.hub.send(conn, envelope(type, payload, seq=seq, reply_to=reply_to))


def _error(ctx: ServerContext, conn: Connection, code: str, message: str, for_seq: int | None = None) -> None:
    payload: dict[str, Any] = {"code": code, "message": message}
    if for_seq is not None:
        payload["forSeq"] = for_seq
    _send(ctx, conn, "server.error", payload, reply_to=for_seq)


def _ack(ctx: ServerContext, conn: Connection, for_seq: int | None) -> None:
    _send(ctx, conn, "edit.ack", {"forSeq": for_seq, "rev": ctx.store.rev}, reply_to=for_seq)


def _reject(ctx: ServerContext, conn: Connection, for_seq: int | None, reason: str, revert: list[dict]) -> None:
    _send(ctx, conn, "edit.reject", {"forSeq": for_seq, "reason": reason, "revert": list(revert or [])}, reply_to=for_seq)


def _conn_by_id(ctx: ServerContext, conn_id: int, fallback: Connection) -> Connection:
    for c in ctx.hub.connections():
        if c.id == conn_id:
            return c
    return fallback


# --------------------------------------------------------------------------- handlers
async def on_hello(ctx: ServerContext, conn: Connection, msg: P.ClientHello) -> None:
    conn.client_id = msg.payload.clientId or conn.client_id
    if msg.payload.protocol != 1:
        _error(ctx, conn, "protocol", f"unsupported protocol {msg.payload.protocol}; server speaks 1", msg.seq)
    snapshot = ctx.store.snapshot().model_dump(mode="json")
    _send(ctx, conn, "plan.snapshot", snapshot, reply_to=msg.seq)
    for ev in ctx.log.read_since(msg.payload.lastSeq, limit=ALL_EVENTS_LIMIT):
        _send(ctx, conn, "event.append", ev.model_dump(), seq=ev.seq)
    _send(ctx, conn, "bridge.status", ctx.bridge.status())


def _commit_edit(ctx: ServerContext, conn: Connection, ops: list[dict], result, for_seq: int | None, *, source: str) -> None:
    """A committed (cosmetic or approved) edit: broadcast, ack, log, push."""
    ctx.bus.publish_messages(result.messages)
    _ack(ctx, conn, for_seq)
    lines = describe_ops(ops)
    note = "; ".join(lines) if lines else (_flat(result.summary) or "canvas edit")
    affected = list(result.affected)
    ctx.log.append(
        agent_id="user",
        node_id=affected[0] if len(affected) == 1 else None,
        type="node_changed",
        note=note,
        data={"source": source, "ops": ops, "affected": affected, "rev": ctx.store.rev},
    )
    prefix = "status" if source == "status" else "edit"
    ctx.bus.push_root([f"{prefix}: {line}" for line in lines] or [f"{prefix}: {note}"])
    if source == "status":
        for op in ops:
            if op_kind(op) == "status-changed" and op.get("status") == "done":
                changed = ctx.store.mark_ready_deps(str(op.get("id")))
                if changed:
                    ctx.bus.publish_messages(ctx.store.last_messages)


async def _apply_ops(ctx: ServerContext, conn: Connection, ops: list[dict], for_seq: int | None, *, source: str) -> None:
    result = ctx.store.apply_ops(ops)
    if result.ok:
        _commit_edit(ctx, conn, ops, result, for_seq, source=source)
        return
    if result.risky and result.request_id:
        rid = result.request_id
        ctx.pending_edits[rid] = PendingEdit(conn.id, for_seq, ops, result.summary)
        _send(
            ctx, conn, "risky_edit.request",
            {"request_id": rid, "summary": result.summary, "diff": result.diff, "affected": list(result.affected)},
            reply_to=for_seq,
        )
        ctx.log.append(
            agent_id="user",
            node_id=result.affected[0] if len(result.affected) == 1 else None,
            type="risky_edit",
            note=_flat(result.summary),
            data={"request_id": rid, "ops": ops, "affected": list(result.affected), "status": "pending"},
        )
        return
    _reject(ctx, conn, for_seq, result.error or "invalid edit", result.revert or ops)


async def on_edit(ctx: ServerContext, conn: Connection, msg: P.CanvasEdit) -> None:
    ops = [dict(op) for op in msg.payload.ops if isinstance(op, dict)]
    if not ops:
        _error(ctx, conn, "bad_payload", "canvas.edit: ops is empty", msg.seq)
        return
    await _apply_ops(ctx, conn, ops, msg.seq, source="canvas")


async def on_node_status(ctx: ServerContext, conn: Connection, msg: P.NodeStatus) -> None:
    ops = [{"op": "status-changed", "id": msg.payload.id, "status": msg.payload.status}]
    await _apply_ops(ctx, conn, ops, msg.seq, source="status")


async def on_layout(ctx: ServerContext, conn: Connection, msg: P.CanvasLayout) -> None:
    try:
        layout = ctx.store.save_layout(msg.payload.diagram, msg.payload.patch)
    except ValueError as exc:
        _error(ctx, conn, "bad_layout", str(exc), msg.seq)
        return
    ctx.bus.publish("layout.update", {"diagram": msg.payload.diagram, "layout": layout}, exclude=conn)


async def on_relayout(ctx: ServerContext, conn: Connection, msg: P.PlanRelayout) -> None:
    diagram = msg.payload.diagram or "hld"
    current = ctx.store.layouts.get(diagram) or layout_mod.default_layout()
    cleared = copy.deepcopy(layout_mod._normalize(current))
    kept_nodes = {nid: e for nid, e in cleared["nodes"].items() if isinstance(e, dict) and e.get("pinned")}
    cleared["nodes"] = kept_nodes
    cleared["edges"] = {
        key: e
        for key, e in cleared["edges"].items()
        if all(part in kept_nodes for part in key.split(layout_mod.EDGE_KEY_SEP, 1))
    }
    ctx.bus.publish("layout.update", {"diagram": diagram, "layout": cleared, "relayout": True})


async def on_chat(ctx: ServerContext, conn: Connection, msg: P.ChatMessage) -> None:
    text = msg.payload.text.strip()
    if not text:
        _error(ctx, conn, "bad_payload", "chat.message: text is empty", msg.seq)
        return
    to = msg.payload.agentId or "root"
    ctx.log.append(
        agent_id="user",
        node_id=msg.payload.nodeId,
        type="chat",
        note=text,
        notified=[to] if to != "root" else [],
        data={"to": to, "nodeId": msg.payload.nodeId},
    )
    ctx.bus.push_root([f"chat (to: {to}): {_q(text, 400)}"])


def ingest_paste(ctx: ServerContext, text: str) -> tuple[str, dict]:
    """Try to ingest pasted text directly. Returns ``(kind, info)`` where kind is
    ``"diagram"``, ``"node"`` or ``"text"`` (nothing ingested)."""
    stripped = text.strip()
    blocks = extract_mermaid_blocks(text)
    mermaid: str | None = None
    if blocks:
        mermaid = blocks[0][2]
    elif re.match(r"^(flowchart|graph)\b", stripped):
        mermaid = stripped
    if mermaid is not None:
        try:
            parse(mermaid)
            diagram = ctx.store.write_diagram("hld", mermaid)
        except (MermaidError, ValueError) as exc:
            return "text", {"error": str(exc)}
        ids = [b.strip() for b in re.findall(r"^\s*(node-[a-z0-9-]+)", diagram.mermaid, flags=re.M)]
        return "diagram", {"diagram": diagram.name, "nodes": sorted(set(ids)), "edges": len(diagram.edges)}
    if stripped.startswith("---"):
        meta, body = split_frontmatter(stripped + ("\n" if not stripped.endswith("\n") else ""))
        if meta:
            node_id = meta.get("id") or (f"node-{slugify(str(meta.get('title')))}" if meta.get("title") else None)
            if node_id:
                try:
                    parsed = Node.from_frontmatter({**meta, "id": node_id}, body.lstrip("\n"))
                    node = ctx.store.upsert_node(
                        id=parsed.id, title=parsed.title, type=parsed.type, status=parsed.status,
                        owner=parsed.owner, depends_on=parsed.depends_on, interfaces=parsed.interfaces,
                        body=parsed.body, diagram="er" if parsed.type == "er" else "hld", **parsed.extra,
                    )
                except (ValueError, TypeError) as exc:
                    return "text", {"error": str(exc)}
                return "node", {"id": node.id, "title": node.title}
    return "text", {}


async def on_paste(ctx: ServerContext, conn: Connection, msg: P.PlanPaste) -> None:
    text = msg.payload.text
    if not text.strip():
        _error(ctx, conn, "bad_payload", "plan.paste: text is empty", msg.seq)
        return
    kind, info = ingest_paste(ctx, text)
    if kind == "diagram":
        ctx.bus.publish_messages(ctx.store.last_messages)
        _ack(ctx, conn, msg.seq)
        ctx.log.append(
            agent_id="user", node_id=None, type="node_changed",
            note=f"pasted diagram {info['diagram']}: {len(info['nodes'])} node(s), {info['edges']} edge(s)",
            data={"source": "paste", **info},
        )
        ctx.bus.push_root([f"plan pasted as diagram {info['diagram']}: {', '.join(info['nodes']) or 'no nodes'}"])
        return
    if kind == "node":
        ctx.bus.publish_messages(ctx.store.last_messages)
        _ack(ctx, conn, msg.seq)
        ctx.log.append(
            agent_id="user", node_id=info["id"], type="node_changed",
            note=f"pasted node {info['id']} ({info['title']})", data={"source": "paste", **info},
        )
        ctx.bus.push_root([f"plan pasted as node {info['id']} ({_q(info['title'])})"])
        return
    ev = ctx.log.append(
        agent_id="user", node_id=None, type="plan_pasted",
        note=" ".join(text.split())[:200],
        data={"text": text, **({"error": info["error"]} if info.get("error") else {})},
    )
    _ack(ctx, conn, msg.seq)
    ctx.bus.push_root([f"plan pasted (event {ev.seq}): structure it with upsert_node/write_diagram"])


async def on_prompt_reply(ctx: ServerContext, conn: Connection, msg: P.PromptReply) -> None:
    prompt = ctx.log.get_prompt(msg.payload.prompt_id)
    try:
        ctx.log.answer_prompt(msg.payload.prompt_id, msg.payload.value)
    except KeyError as exc:
        _error(ctx, conn, "unknown_prompt", str(exc), msg.seq)
        return
    _ack(ctx, conn, msg.seq)
    agent = (prompt or {}).get("agent_id") or "?"
    ctx.bus.push_root([f"needs_input reply (prompt {msg.payload.prompt_id}, {agent}): {_q(msg.payload.value, 400)}"])


def _find_event(ctx: ServerContext, type_: str, request_id: str):
    for ev in reversed(ctx.log.read_since(0, limit=ALL_EVENTS_LIMIT)):
        if ev.type == type_ and ev.data.get("request_id") == request_id:
            return ev
    return None


async def on_dispatch_reply(ctx: ServerContext, conn: Connection, msg: P.DispatchReply) -> None:
    rid = msg.payload.request_id
    proposed = _find_event(ctx, "dispatch_proposed", rid)
    if proposed is None:
        _error(ctx, conn, "unknown_request", f"no dispatch_proposed event with request_id {rid!r}", msg.seq)
        return
    node_id = proposed.data.get("node_id") or proposed.node_id
    agent_id = proposed.data.get("agent_id") or proposed.agent_id
    note = (msg.payload.note or "").strip()
    approved = bool(msg.payload.approved)
    ctx.log.append(
        agent_id="user",
        node_id=node_id,
        type="dispatch_approved" if approved else "dispatch_rejected",
        note=note,
        notified=[agent_id] if agent_id else [],
        data={"request_id": rid, "node_id": node_id, "agent_id": agent_id, "note": note},
    )
    _ack(ctx, conn, msg.seq)
    if approved:
        node = ctx.store.get_node(node_id) if node_id else None
        if node is not None and agent_id and node.owner != agent_id:
            try:
                ctx.store.set_node_status(node_id, node.status, owner=agent_id)
                ctx.bus.publish_messages(ctx.store.last_messages)
            except ValueError as exc:
                log.warning("could not set owner of %s to %s: %s", node_id, agent_id, exc)
        ctx.bus.push_root([
            f"dispatch approved: {node_id} → {agent_id} (spawn it now with subagent_type=whiteboard-task; "
            f"job spec: .whiteboard/agents/{agent_id}/plan.md)" + (f" — {_q(note)}" if note else "")
        ])
    else:
        ctx.bus.push_root([f"dispatch rejected: {node_id} → {agent_id}" + (f" — {_q(note)}" if note else "")])


async def on_risky_reply(ctx: ServerContext, conn: Connection, msg: P.RiskyEditReply) -> None:
    rid = msg.payload.request_id
    note = (msg.payload.note or "").strip()
    pending = ctx.pending_edits.pop(rid, None)
    origin = _conn_by_id(ctx, pending.conn_id, conn) if pending else conn
    for_seq = pending.for_seq if pending else None
    result = ctx.store.resolve_pending(rid, bool(msg.payload.approved), note)
    if result.error and not result.risky:
        _error(ctx, conn, "unknown_request", result.error, msg.seq)
        return
    summary = _flat(result.summary or (pending.summary if pending else ""))
    suffix = f" — {_q(note)}" if note else ""
    affected = list(result.affected)
    if result.ok:
        ctx.bus.publish_messages(result.messages)
        _ack(ctx, origin, for_seq)
        if origin is not conn:
            _ack(ctx, conn, msg.seq)
        ctx.log.append(
            agent_id="user", node_id=affected[0] if len(affected) == 1 else None, type="risky_edit_accepted",
            note=f"{summary}{suffix}", data={"request_id": rid, "affected": affected, "note": note, "rev": ctx.store.rev},
        )
        ctx.bus.push_root([f"risky edit accepted (req {rid}): {summary}{suffix}"])
        return
    reason = result.error or ("rejected by user" + (f": {note}" if note else ""))
    _reject(ctx, origin, for_seq, reason, result.revert)
    if origin is not conn:
        _ack(ctx, conn, msg.seq)
    ctx.log.append(
        agent_id="user", node_id=affected[0] if len(affected) == 1 else None, type="risky_edit_rejected",
        note=f"{summary}{suffix}" if not result.error else f"{summary} — failed: {result.error}",
        data={"request_id": rid, "affected": affected, "note": note, "error": result.error},
    )
    ctx.bus.push_root([f"risky edit rejected (req {rid}): {summary}{suffix}"])


HANDLERS = {
    "client.hello": on_hello,
    "canvas.edit": on_edit,
    "canvas.layout": on_layout,
    "node.status": on_node_status,
    "chat.message": on_chat,
    "plan.paste": on_paste,
    "prompt.reply": on_prompt_reply,
    "dispatch.reply": on_dispatch_reply,
    "risky_edit.reply": on_risky_reply,
    "plan.relayout": on_relayout,
}


async def dispatch(ctx: ServerContext, conn: Connection, raw: Any) -> None:
    """Parse one client message and run its handler; every failure becomes ``server.error``."""
    try:
        msg = P.parse_client_message(raw)
    except P.ProtocolError as exc:
        _error(ctx, conn, exc.code, exc.message, exc.for_seq)
        return
    handler = HANDLERS.get(msg.type)
    if handler is None:  # pragma: no cover - the parser already rejects unknown types
        _error(ctx, conn, "unknown_type", f"unknown message type {msg.type!r}", msg.seq)
        return
    try:
        await handler(ctx, conn, msg)
    except Exception as exc:
        log.exception("handler for %s failed", msg.type)
        _error(ctx, conn, "internal", f"{type(exc).__name__}: {exc}", msg.seq)


async def serve_websocket(ctx: ServerContext, ws: WebSocket) -> None:
    """The ``/ws`` endpoint body: origin check, accept, register, read loop."""
    origin = ws.headers.get("origin")
    if not origin_allowed(origin):
        log.warning("rejecting websocket from origin %r", origin)
        await ws.close(code=1008, reason="origin not allowed")
        return
    await ws.accept()
    conn = ctx.hub.register(ws)
    log.info("websocket client %s connected (%s clients)", conn.id, ctx.hub.clients)
    try:
        while True:
            text = await ws.receive_text()
            try:
                raw = json.loads(text)
            except ValueError:
                _error(ctx, conn, "bad_json", "message is not valid JSON")
                continue
            await dispatch(ctx, conn, raw)
    except WebSocketDisconnect:
        pass
    except RuntimeError as exc:
        # Starlette raises this when receive() is called after a disconnect frame.
        log.debug("websocket %s ended: %s", conn.id, exc)
    finally:
        await ctx.hub.unregister(conn)
        log.info("websocket client %s disconnected (%s clients)", conn.id, ctx.hub.clients)
