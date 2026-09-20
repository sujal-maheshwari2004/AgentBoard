"""HTTP routes (CONTRACTS §10/§11, docs/research/python-server.md "Logging / debug endpoints").

Everything reads ``request.app.state.ctx`` (a :class:`~whiteboard.server.bus.ServerContext`).
The canvas index, ``/assets`` and the SPA fallback live in ``app.py`` because they
depend on the mount order.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from whiteboard.server import handlers
from whiteboard.server.bus import ServerContext

__all__ = ["router", "ctx_of"]

log = logging.getLogger(__name__)

router = APIRouter()


def ctx_of(request: Request) -> ServerContext:
    return request.app.state.ctx


class SessionBody(BaseModel):
    model_config = ConfigDict(extra="ignore")

    socket: str | None = None
    token: str | None = None


class PasteBody(BaseModel):
    model_config = ConfigDict(extra="ignore")

    text: str


@router.get("/api/health")
async def health(request: Request) -> dict:
    return ctx_of(request).health()


@router.get("/api/events")
async def events(request: Request, since: int = 0, limit: int = 500) -> dict:
    ctx = ctx_of(request)
    limit = max(0, min(int(limit), 5000))
    items = ctx.log.read_since(int(since), limit=limit)
    return {
        "events": [e.model_dump() for e in items],
        "latest_seq": ctx.log.latest_seq,
        "since": int(since),
        "bridge": {"last_pushed_seq": ctx.bridge.last_pushed_seq},
    }


@router.post("/api/session")
async def session(request: Request, body: SessionBody) -> dict:
    """Re-target the root-session bridge (``session_hook.sh start`` / ``enter.sh``)."""
    ctx = ctx_of(request)
    ctx.bridge.retarget(body.socket or None, body.token)
    status = ctx.bridge.status()
    ctx.bus.publish("bridge.status", status)
    return {"ok": True, "bridge": status}


@router.post("/api/paste")
async def paste(request: Request, body: PasteBody) -> dict:
    """Same as the websocket ``plan.paste``."""
    ctx = ctx_of(request)
    if not body.text.strip():
        raise HTTPException(status_code=400, detail="text is empty")
    kind, info = handlers.ingest_paste(ctx, body.text)
    if kind in ("diagram", "node"):
        ctx.bus.publish_messages(ctx.store.last_messages)
        note = (
            f"pasted diagram {info['diagram']}: {len(info['nodes'])} node(s), {info['edges']} edge(s)"
            if kind == "diagram"
            else f"pasted node {info['id']} ({info['title']})"
        )
        ev = ctx.log.append(agent_id="user", node_id=info.get("id"), type="node_changed", note=note, data={"source": "paste", **info})
        ctx.bus.push_root([f"plan pasted as {kind} {info.get('diagram') or info.get('id')}"])
        return {"ingested": kind, "event_seq": ev.seq, **info}
    ev = ctx.log.append(
        agent_id="user", node_id=None, type="plan_pasted", note=" ".join(body.text.split())[:200],
        data={"text": body.text, **({"error": info["error"]} if info.get("error") else {})},
    )
    ctx.bus.push_root([f"plan pasted (event {ev.seq}): structure it with upsert_node/write_diagram"])
    return {"ingested": "text", "event_seq": ev.seq, **info}


@router.get("/api/debug/state")
async def debug_state(request: Request) -> dict:
    ctx = ctx_of(request)
    return {
        "snapshot": ctx.store.snapshot().model_dump(mode="json"),
        "last_good": ctx.store.last_good,
        "invalid": dict(ctx.store.invalid),
        "pending_edits": {rid: _jsonable(p) for rid, p in ctx.store.pending_edits.items()},
        "pending_prompts": {pid: dict(p) for pid, p in ctx.log.pending_prompts.items()},
        "bridge": ctx.bridge.status(),
        "clients": ctx.hub.clients,
    }


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


@router.get("/skeleton")
async def skeleton(request: Request) -> dict:
    return ctx_of(request).store.skeleton().model_dump(mode="json")


@router.get("/api/snapshot")
async def snapshot(request: Request) -> dict:
    return ctx_of(request).store.snapshot().model_dump(mode="json")


@router.get("/api/nodes/{node_id}")
async def node(request: Request, node_id: str) -> dict:
    found = ctx_of(request).store.get_node(node_id)
    if found is None:
        raise HTTPException(status_code=404, detail=f"unknown node {node_id!r}")
    return found.model_dump(mode="json")
