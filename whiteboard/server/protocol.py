"""Client -> server websocket messages as pydantic models (CONTRACTS §6).

``parse_client_message(raw)`` validates the envelope ``{type, payload, seq,
ts?, replyTo?}`` and the per-type payload; it raises :class:`ProtocolError`
(``code`` + ``message``) which the socket loop turns into ``server.error``.
Unknown fields in payloads are ignored so a newer canvas keeps working.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

__all__ = [
    "ProtocolError",
    "ClientMessage",
    "ClientHello",
    "CanvasEdit",
    "CanvasLayout",
    "NodeStatus",
    "ChatMessage",
    "PlanPaste",
    "PromptReply",
    "DispatchReply",
    "RiskyEditReply",
    "PlanRelayout",
    "parse_client_message",
    "CLIENT_MESSAGE_TYPES",
]


class ProtocolError(ValueError):
    """A client message we cannot accept; ``code`` is machine-readable."""

    def __init__(self, code: str, message: str, for_seq: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.for_seq = for_seq


class _Payload(BaseModel):
    model_config = ConfigDict(extra="ignore")


class HelloPayload(_Payload):
    clientId: str = ""
    lastSeq: int = 0
    protocol: int = 1


class EditPayload(_Payload):
    ops: list[dict[str, Any]] = Field(default_factory=list)


class LayoutPayload(_Payload):
    diagram: str = "hld"
    patch: dict[str, Any] = Field(default_factory=dict)


class NodeStatusPayload(_Payload):
    id: str
    status: str


class ChatPayload(_Payload):
    text: str
    agentId: str | None = None
    nodeId: str | None = None


class PastePayload(_Payload):
    text: str


class PromptReplyPayload(_Payload):
    prompt_id: str
    value: Any = None


class DispatchReplyPayload(_Payload):
    request_id: str
    approved: bool
    note: str | None = None


class RiskyEditReplyPayload(_Payload):
    request_id: str
    approved: bool
    note: str = ""


class RelayoutPayload(_Payload):
    diagram: str = "hld"


class _Envelope(BaseModel):
    """Common envelope fields; ``seq`` is the client's per-connection counter."""

    model_config = ConfigDict(extra="ignore")

    seq: int | None = None
    ts: str | None = None
    replyTo: int | None = None


class ClientHello(_Envelope):
    type: Literal["client.hello"]
    payload: HelloPayload = Field(default_factory=HelloPayload)


class CanvasEdit(_Envelope):
    type: Literal["canvas.edit"]
    payload: EditPayload


class CanvasLayout(_Envelope):
    type: Literal["canvas.layout"]
    payload: LayoutPayload


class NodeStatus(_Envelope):
    type: Literal["node.status"]
    payload: NodeStatusPayload


class ChatMessage(_Envelope):
    type: Literal["chat.message"]
    payload: ChatPayload


class PlanPaste(_Envelope):
    type: Literal["plan.paste"]
    payload: PastePayload


class PromptReply(_Envelope):
    type: Literal["prompt.reply"]
    payload: PromptReplyPayload


class DispatchReply(_Envelope):
    type: Literal["dispatch.reply"]
    payload: DispatchReplyPayload


class RiskyEditReply(_Envelope):
    type: Literal["risky_edit.reply"]
    payload: RiskyEditReplyPayload


class PlanRelayout(_Envelope):
    type: Literal["plan.relayout"]
    payload: RelayoutPayload = Field(default_factory=RelayoutPayload)


ClientMessage = Annotated[
    Union[
        ClientHello,
        CanvasEdit,
        CanvasLayout,
        NodeStatus,
        ChatMessage,
        PlanPaste,
        PromptReply,
        DispatchReply,
        RiskyEditReply,
        PlanRelayout,
    ],
    Field(discriminator="type"),
]

CLIENT_MESSAGE_TYPES: tuple[str, ...] = (
    "client.hello",
    "canvas.edit",
    "canvas.layout",
    "node.status",
    "chat.message",
    "plan.paste",
    "prompt.reply",
    "dispatch.reply",
    "risky_edit.reply",
    "plan.relayout",
)

_adapter: TypeAdapter[Any] = TypeAdapter(ClientMessage)


def _seq_of(raw: Any) -> int | None:
    if isinstance(raw, dict) and isinstance(raw.get("seq"), int):
        return raw["seq"]
    return None


def parse_client_message(raw: Any) -> Any:
    """Validate one decoded JSON object into its typed message model."""
    if not isinstance(raw, dict):
        raise ProtocolError("bad_envelope", "message must be a JSON object")
    mtype = raw.get("type")
    if not isinstance(mtype, str) or not mtype:
        raise ProtocolError("bad_envelope", "message has no 'type'", _seq_of(raw))
    if mtype not in CLIENT_MESSAGE_TYPES:
        raise ProtocolError("unknown_type", f"unknown message type {mtype!r}", _seq_of(raw))
    if "payload" in raw and raw["payload"] is None:
        raw = {**raw, "payload": {}}
    try:
        return _adapter.validate_python(raw)
    except ValidationError as exc:
        first = exc.errors()[0] if exc.errors() else {}
        loc = ".".join(str(p) for p in first.get("loc", ()) if p != "payload") or "payload"
        raise ProtocolError("bad_payload", f"{mtype}: invalid {loc}: {first.get('msg', 'invalid')}", _seq_of(raw)) from exc
