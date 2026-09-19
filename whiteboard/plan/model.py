"""Plan data models (pydantic v2). See docs/CONTRACTS.md §4."""

from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

NodeType = Literal["hld", "lld", "er"]
NodeStatus = Literal["todo", "in_progress", "blocked", "done"]
AgentStatus = Literal["idle", "working", "blocked", "done"]

NODE_ID_RE = re.compile(r"^node-[a-z0-9][a-z0-9-]*$")
AGENT_ID_RE = re.compile(r"^agent-[a-z0-9][a-z0-9-]*$")

NODE_TYPES: tuple[str, ...] = ("hld", "lld", "er")
NODE_STATUSES: tuple[str, ...] = ("todo", "in_progress", "blocked", "done")
AGENT_STATUSES: tuple[str, ...] = ("idle", "working", "blocked", "done")

NODE_FRONTMATTER_KEYS: tuple[str, ...] = (
    "id", "type", "title", "status", "owner", "depends_on", "interfaces",
)
AGENT_FRONTMATTER_KEYS: tuple[str, ...] = (
    "id", "assigned_node", "status", "claude_agent_ref", "ready_deps", "spawned_at",
)


def validate_node_id(id: str) -> str:
    """Return `id` if it matches ^node-[a-z0-9][a-z0-9-]*$, else raise ValueError."""
    if not isinstance(id, str) or not NODE_ID_RE.match(id):
        raise ValueError(f"invalid node id {id!r}: must match {NODE_ID_RE.pattern}")
    return id


def validate_agent_id(id: str) -> str:
    """Return `id` if it matches ^agent-[a-z0-9][a-z0-9-]*$, else raise ValueError."""
    if not isinstance(id, str) or not AGENT_ID_RE.match(id):
        raise ValueError(f"invalid agent id {id!r}: must match {AGENT_ID_RE.pattern}")
    return id


def slugify(title: str) -> str:
    """Turn free text into a valid id suffix: lowercase ascii, `-` separated, never empty.

    `slugify("Mermaid parser v2") == "mermaid-parser-v2"`; `f"node-{slugify(t)}"` is always a valid node id.
    """
    text = unicodedata.normalize("NFKD", str(title or "")).encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    text = re.sub(r"-{2,}", "-", text)
    if not text:
        return "untitled"
    return text


def _none_like(v: Any) -> bool:
    return v is None or v == "" or v == "null"


def _dedupe_ids(values: Any, validator) -> list[str]:
    if values is None:
        return []
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, (list, tuple, set, frozenset)):
        raise ValueError(f"expected a list of ids, got {type(values).__name__}")
    out: list[str] = []
    seen: set[str] = set()
    for raw in values:
        if raw is None:
            continue
        v = validator(str(raw).strip())
        if v not in seen:
            seen.add(v)
            out.append(v)
    return out


def _normalize_interfaces(values: Any) -> list[str | dict]:
    if values is None:
        return []
    if isinstance(values, (str, dict)):
        values = [values]
    if not isinstance(values, (list, tuple)):
        raise ValueError("interfaces must be a list of strings or {name, kind} objects")
    out: list[str | dict] = []
    for item in values:
        if item is None:
            continue
        if isinstance(item, dict):
            out.append(dict(item))
        else:
            s = str(item).strip()
            if s:
                out.append(s)
    return out


def interface_text(item: str | dict) -> str:
    """Stable one-line rendering of an interface entry (string or {name, kind})."""
    if isinstance(item, dict):
        name = str(item.get("name", "") or "").strip()
        kind = str(item.get("kind", "") or "").strip()
        if name and kind:
            return f"{name} ({kind})"
        if name:
            return name
        return json.dumps(item, sort_keys=True, default=str)
    return str(item)


class Node(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    id: str
    type: NodeType = "hld"
    title: str
    status: NodeStatus = "todo"
    owner: str | None = None
    depends_on: list[str] = Field(default_factory=list)
    interfaces: list[str | dict] = Field(default_factory=list)
    body: str = ""
    extra: dict = Field(default_factory=dict)
    path: str | None = None

    @field_validator("id")
    @classmethod
    def _check_id(cls, v: str) -> str:
        return validate_node_id(v)

    @field_validator("title", mode="before")
    @classmethod
    def _coerce_title(cls, v: Any) -> Any:
        return "" if v is None else str(v)

    @field_validator("owner", mode="before")
    @classmethod
    def _check_owner(cls, v: Any) -> str | None:
        if _none_like(v):
            return None
        return validate_agent_id(str(v).strip())

    @field_validator("depends_on", mode="before")
    @classmethod
    def _check_deps(cls, v: Any) -> list[str]:
        return _dedupe_ids(v, validate_node_id)

    @field_validator("interfaces", mode="before")
    @classmethod
    def _check_interfaces(cls, v: Any) -> list[str | dict]:
        return _normalize_interfaces(v)

    @field_validator("body", mode="before")
    @classmethod
    def _coerce_body(cls, v: Any) -> str:
        return "" if v is None else str(v)

    @field_validator("extra", mode="before")
    @classmethod
    def _coerce_extra(cls, v: Any) -> dict:
        return {} if v is None else dict(v)

    @field_validator("path", mode="before")
    @classmethod
    def _coerce_path(cls, v: Any) -> str | None:
        return None if v is None else str(v)

    def frontmatter(self) -> dict:
        """Ordered frontmatter mapping: id, type, title, status, owner, depends_on, interfaces, then extra keys."""
        meta: dict[str, Any] = {
            "id": self.id,
            "type": self.type,
            "title": self.title,
            "status": self.status,
            "owner": self.owner,
            "depends_on": list(self.depends_on),
            "interfaces": [dict(i) if isinstance(i, dict) else i for i in self.interfaces],
        }
        for k, v in self.extra.items():
            if k not in meta:
                meta[k] = v
        return meta

    @classmethod
    def from_frontmatter(cls, meta: dict, body: str, path: str | None = None) -> "Node":
        """Tolerant constructor from a parsed node file.

        Missing `id` falls back to the file stem; missing `title` falls back to the id;
        `null` type/status fall back to defaults; unknown keys are kept in `extra`.
        """
        meta = dict(meta or {})
        node_id = meta.pop("id", None)
        if _none_like(node_id) and path:
            node_id = Path(path).stem
        fields: dict[str, Any] = {"id": node_id}
        for key in ("type", "status", "owner", "depends_on", "interfaces"):
            if key in meta:
                val = meta.pop(key)
                if key in ("type", "status") and _none_like(val):
                    continue
                fields[key] = val
        title = meta.pop("title", None)
        if title is None or str(title).strip() == "":
            title = node_id
        fields["title"] = str(title)
        fields["extra"] = meta
        fields["body"] = body or ""
        fields["path"] = path
        return cls(**fields)

    def is_done(self) -> bool:
        return self.status == "done"


class Edge(BaseModel):
    model_config = ConfigDict(extra="forbid")

    src: str
    dst: str
    label: str | None = None
    diagram: str = "hld"


class AgentCard(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    id: str
    assigned_node: str | None = None
    status: AgentStatus = "idle"
    claude_agent_ref: str | None = None
    ready_deps: list[str] = Field(default_factory=list)
    spawned_at: str | None = None
    notes: str = ""
    plan_md: str = ""
    diagrams_md: str = ""
    extra: dict = Field(default_factory=dict)

    @field_validator("id")
    @classmethod
    def _check_id(cls, v: str) -> str:
        return validate_agent_id(v)

    @field_validator("assigned_node", mode="before")
    @classmethod
    def _check_assigned(cls, v: Any) -> str | None:
        if _none_like(v):
            return None
        return validate_node_id(str(v).strip())

    @field_validator("claude_agent_ref", mode="before")
    @classmethod
    def _coerce_ref(cls, v: Any) -> str | None:
        return None if _none_like(v) else str(v)

    @field_validator("spawned_at", mode="before")
    @classmethod
    def _coerce_spawned(cls, v: Any) -> str | None:
        if _none_like(v):
            return None
        if hasattr(v, "isoformat"):
            return v.isoformat()
        return str(v)

    @field_validator("ready_deps", mode="before")
    @classmethod
    def _check_ready(cls, v: Any) -> list[str]:
        return _dedupe_ids(v, validate_node_id)

    @field_validator("notes", "plan_md", "diagrams_md", mode="before")
    @classmethod
    def _coerce_text(cls, v: Any) -> str:
        return "" if v is None else str(v)

    @field_validator("extra", mode="before")
    @classmethod
    def _coerce_extra(cls, v: Any) -> dict:
        return {} if v is None else dict(v)

    def frontmatter(self) -> dict:
        """Ordered card frontmatter: id, assigned_node, status, claude_agent_ref, ready_deps, spawned_at, then extra keys."""
        meta: dict[str, Any] = {
            "id": self.id,
            "assigned_node": self.assigned_node,
            "status": self.status,
            "claude_agent_ref": self.claude_agent_ref,
            "ready_deps": list(self.ready_deps),
            "spawned_at": self.spawned_at,
        }
        for k, v in self.extra.items():
            if k not in meta:
                meta[k] = v
        return meta

    @classmethod
    def from_frontmatter(
        cls, meta: dict, body: str, plan_md: str = "", diagrams_md: str = ""
    ) -> "AgentCard":
        """Tolerant constructor from card.md frontmatter + body (notes) and sibling files."""
        meta = dict(meta or {})
        fields: dict[str, Any] = {"id": meta.pop("id", None)}
        for key in ("assigned_node", "status", "claude_agent_ref", "ready_deps", "spawned_at"):
            if key in meta:
                val = meta.pop(key)
                if key == "status" and _none_like(val):
                    continue
                fields[key] = val
        fields["extra"] = meta
        fields["notes"] = body or ""
        fields["plan_md"] = plan_md or ""
        fields["diagrams_md"] = diagrams_md or ""
        return cls(**fields)


class Event(BaseModel):
    """One events.jsonl record. `model_dump()` is JSON-safe (only str/int/list/dict/None)."""

    model_config = ConfigDict(extra="ignore")

    seq: int
    ts: str
    agent_id: str
    node_id: str | None = None
    type: str
    note: str = ""
    notified: list[str] = Field(default_factory=list)
    data: dict = Field(default_factory=dict)

    @field_validator("note", mode="before")
    @classmethod
    def _coerce_note(cls, v: Any) -> str:
        return "" if v is None else str(v)

    @field_validator("notified", mode="before")
    @classmethod
    def _coerce_notified(cls, v: Any) -> list[str]:
        if v is None:
            return []
        if isinstance(v, str):
            return [v]
        return [str(x) for x in v]

    @field_validator("data", mode="before")
    @classmethod
    def _coerce_data(cls, v: Any) -> dict:
        return {} if v is None else dict(v)

    def model_dump(self, **kwargs: Any) -> dict:  # type: ignore[override]
        kwargs.setdefault("mode", "json")
        return super().model_dump(**kwargs)


class Skeleton(BaseModel):
    """Cross-project summary: [{id, title, type, status, depends_on}]."""

    model_config = ConfigDict(extra="ignore")

    project: str
    nodes: list[dict] = Field(default_factory=list)


class Diagram(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str
    direction: str = "TD"
    edges: list[Edge] = Field(default_factory=list)
    mermaid: str = ""
    path: str = ""


class PlanSnapshot(BaseModel):
    model_config = ConfigDict(extra="ignore")

    project: str
    rev: int = 0
    nodes: list[Node] = Field(default_factory=list)
    edges: list[Edge] = Field(default_factory=list)
    agents: list[AgentCard] = Field(default_factory=list)
    diagrams: list[Diagram] = Field(default_factory=list)
    layout: dict[str, dict] = Field(default_factory=dict)


def node_ready(node: Node, nodes: dict[str, Node]) -> bool:
    """True when every dependency exists and is done (an empty depends_on is ready)."""
    for dep in node.depends_on:
        other = nodes.get(dep)
        if other is None or other.status != "done":
            return False
    return True


__all__ = [
    "AGENT_FRONTMATTER_KEYS", "AGENT_ID_RE", "AGENT_STATUSES", "AgentCard", "AgentStatus",
    "Diagram", "Edge", "Event", "NODE_FRONTMATTER_KEYS", "NODE_ID_RE", "NODE_STATUSES",
    "NODE_TYPES", "Node", "NodeStatus", "NodeType", "PlanSnapshot", "Skeleton",
    "interface_text", "node_ready", "slugify", "validate_agent_id", "validate_node_id",
]
