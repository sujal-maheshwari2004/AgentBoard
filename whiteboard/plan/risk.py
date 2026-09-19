"""Risky-edit classification: semantic node diffs, scores, graph checks. See docs/CONTRACTS.md §4."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from whiteboard.plan.graph import Graph
from whiteboard.plan.model import Node, interface_text

WEIGHT_TYPE = 100
WEIGHT_DEPENDS_ON = 40
WEIGHT_INTERFACE = 30
WEIGHT_STATUS = 5
WEIGHT_DELETED = 100
REMOVAL_MULTIPLIER = 2
DEFAULT_THRESHOLD = 30

COSMETIC_OP_KINDS: frozenset[str] = frozenset({"renamed", "node-created", "status-changed"})
RISKY_OP_KINDS: frozenset[str] = frozenset({"deleted", "edge-created", "edge-deleted", "edge-rerouted"})


@dataclass(frozen=True)
class NodeSemantics:
    node_id: str
    type: str
    status: str
    depends_on: frozenset[str]
    interfaces: tuple[str, ...]

    @classmethod
    def from_node(cls, node: Node) -> "NodeSemantics":
        return cls(
            node_id=node.id,
            type=node.type,
            status=node.status,
            depends_on=frozenset(node.depends_on),
            interfaces=tuple(interface_text(i) for i in node.interfaces),
        )


@dataclass
class Change:
    field: str
    added: tuple = ()
    removed: tuple = ()
    before: object = None
    after: object = None
    weight: int = 0


def diff_node(old: NodeSemantics | None, new: NodeSemantics | None) -> list[Change]:
    """Semantic changes between two states. `old is None` -> created (weight 0);
    `new is None` -> deleted (weight 100, always risky)."""
    if old is None and new is None:
        return []
    if old is None:
        assert new is not None
        return [Change(field="created", after=new.node_id, weight=0)]
    if new is None:
        return [Change(field="deleted", before=old.node_id, weight=WEIGHT_DELETED)]

    changes: list[Change] = []
    if old.type != new.type:
        changes.append(Change(field="type", before=old.type, after=new.type, weight=WEIGHT_TYPE))

    added = tuple(sorted(new.depends_on - old.depends_on))
    removed = tuple(sorted(old.depends_on - new.depends_on))
    if added or removed:
        changes.append(
            Change(
                field="depends_on",
                added=added,
                removed=removed,
                before=tuple(sorted(old.depends_on)),
                after=tuple(sorted(new.depends_on)),
                weight=WEIGHT_DEPENDS_ON * (len(added) + REMOVAL_MULTIPLIER * len(removed)),
            )
        )

    old_ifaces, new_ifaces = set(old.interfaces), set(new.interfaces)
    added = tuple(i for i in new.interfaces if i not in old_ifaces)
    removed = tuple(i for i in old.interfaces if i not in new_ifaces)
    if added or removed:
        changes.append(
            Change(
                field="interfaces",
                added=added,
                removed=removed,
                before=old.interfaces,
                after=new.interfaces,
                weight=WEIGHT_INTERFACE * (len(added) + REMOVAL_MULTIPLIER * len(removed)),
            )
        )

    if old.status != new.status:
        changes.append(Change(field="status", before=old.status, after=new.status, weight=WEIGHT_STATUS))
    return changes


def risk_score(changes: Iterable[Change]) -> int:
    return sum(c.weight for c in changes)


def is_risky(changes: Iterable[Change], threshold: int = DEFAULT_THRESHOLD) -> bool:
    changes = list(changes)
    if any(c.field == "deleted" for c in changes):
        return True
    return risk_score(changes) >= threshold


def _fmt_seq(items: Iterable[object]) -> str:
    return ", ".join(str(i) for i in items)


def describe(changes: Iterable[Change]) -> str:
    """One human-readable line per change, newline-joined."""
    lines: list[str] = []
    for c in changes:
        if c.field == "created":
            lines.append(f"created {c.after}")
        elif c.field == "deleted":
            lines.append(f"deleted {c.before}")
        elif c.field in ("depends_on", "interfaces"):
            parts: list[str] = []
            if c.added:
                parts.append("+" + _fmt_seq(c.added))
            if c.removed:
                parts.append("-" + _fmt_seq(c.removed))
            lines.append(f"{c.field} {' '.join(parts)}".rstrip())
        else:
            lines.append(f"{c.field} {c.before!r} -> {c.after!r}")
    return "\n".join(lines) if lines else "no semantic changes"


def graph_problems(nodes: dict[str, Node]) -> list[str]:
    """Human-readable strings for unknown dependencies and cycles (empty when the graph is sound)."""
    g = Graph(nodes)
    problems: list[str] = []
    for nid, missing in g.unknown_deps().items():
        problems.append(f"{nid} depends on unknown node(s): {_fmt_seq(missing)}")
    for cyc in g.cycles():
        problems.append("dependency cycle: " + " -> ".join(cyc + cyc[:1]))
    return problems


def op_kind(op: dict) -> str:
    """The semantic op kind, read from `op`, `kind` or `type` (in that order)."""
    for key in ("op", "kind", "type"):
        v = op.get(key)
        if isinstance(v, str) and v:
            return v
    return ""


def classify_ops(ops: list[dict]) -> tuple[list[dict], list[dict]]:
    """Split canvas ops into (cosmetic, risky). Unknown kinds are treated as risky."""
    cosmetic: list[dict] = []
    risky: list[dict] = []
    for op in ops:
        kind = op_kind(op) if isinstance(op, dict) else ""
        (cosmetic if kind in COSMETIC_OP_KINDS else risky).append(op)
    return cosmetic, risky


__all__ = [
    "COSMETIC_OP_KINDS", "Change", "DEFAULT_THRESHOLD", "NodeSemantics", "RISKY_OP_KINDS",
    "classify_ops", "describe", "diff_node", "graph_problems", "is_risky", "op_kind", "risk_score",
]
