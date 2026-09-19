"""Deterministic serializer for :class:`MermaidDoc` (docs/CONTRACTS.md §3).

Output layout::

    <passthrough lines flagged before_header>
    <kind> <direction>
    <passthroughs with after_statement == -1>
    <top-level node declarations, first-seen order>
    <subgraph blocks (members declared inside), nested recursively>
    <edges, declaration order>

Every passthrough line is re-inserted right after the last emitted statement
whose serial (``order``) is the greatest one ``<= after_statement``.  Bare
top-level nodes that appear in an edge are implied by the edge and get no line
of their own.  Operators are canonical (``-->``, ``-.->``, ``==>``, ``---``,
``--o``, ``--x``), lengthened by repeating the middle character according to
``Edge.length``.  Labels are quoted when ``Node.quoted`` is set or the label
matches ``[\\[\\](){}|"<>&#]|^\\s|\\s$``; a ``"`` inside a label is written as
``#quot;``.
"""

from __future__ import annotations

import re

from .ast import Edge, MermaidDoc, Node, Subgraph
from .parser import SHAPE_DELIMS

NEEDS_QUOTE_RE = re.compile(r'[\[\](){}|"<>&#]|^\s|\s$')
_HEAD_CHAR = {"arrow": ">", "circle": "o", "cross": "x", "none": ""}


def needs_quote(label: str) -> bool:
    return bool(NEEDS_QUOTE_RE.search(label))


def render_label(label: str, quoted: bool = False) -> str:
    if quoted or needs_quote(label):
        return '"' + label.replace('"', "#quot;") + '"'
    return label


def render_node(node: Node) -> str:
    if node.shape == "bare" or node.shape not in SHAPE_DELIMS:
        text = node.id
    else:
        open_, close = SHAPE_DELIMS[node.shape]
        label = node.label if node.label is not None else node.id
        text = f"{node.id}{open_}{render_label(label, node.quoted)}{close}"
    return text + "".join(f":::{c}" for c in node.classes)


def _operator(edge: Edge) -> tuple[str, str, str]:
    """Return (plain operator, inline opener, inline closer)."""
    head = _HEAD_CHAR.get(edge.head, ">")
    length = max(1, edge.length)
    if edge.style == "dotted":
        op = "-" + "." * length + "-" + head
        return op, "-.", "." * length + "-" + head
    ch = "=" if edge.style == "thick" else "-"
    op = ch * (length + 1) + head if head else ch * (length + 2)
    return op, ch * 2, op


def render_edge(edge: Edge) -> str:
    op, opener, closer = _operator(edge)
    if edge.label is None:
        return f"{edge.src} {op} {edge.dst}"
    label = render_label(edge.label)
    if edge.label_form == "inline":
        return f"{edge.src} {opener} {label} {closer} {edge.dst}"
    return f"{edge.src} {op}|{label}| {edge.dst}"


def render_subgraph_header(sg: Subgraph) -> str:
    if sg.id is not None and sg.title is not None:
        return f"subgraph {sg.id} [{render_label(sg.title)}]"
    if sg.id is not None:
        return f"subgraph {sg.id}"
    if sg.title is not None:
        return f"subgraph {render_label(sg.title)}"
    return f"subgraph sg{sg.order}"


_Line = tuple[int, str, int | None]  # (depth, text, statement serial)


def _emit_subgraph(doc: MermaidDoc, idx: int, depth: int, children: dict[int | None, list[int]], out: list[_Line]) -> None:
    sg = doc.subgraphs[idx]
    out.append((depth, render_subgraph_header(sg), None))
    if sg.direction:
        out.append((depth + 1, f"direction {sg.direction}", None))
    for member in sg.members:
        node = doc.nodes.get(member)
        if node is None:
            out.append((depth + 1, member, None))
        else:
            out.append((depth + 1, render_node(node), node.order))
    for child in children.get(idx, []):
        _emit_subgraph(doc, child, depth + 1, children, out)
    out.append((depth, "end", None))


def serialize(doc: MermaidDoc) -> str:
    lines: list[_Line] = []
    in_subgraph: set[str] = set()
    for sg in doc.subgraphs:
        in_subgraph.update(sg.members)
    referenced = doc.referenced_ids()

    for node in doc.nodes.values():
        if node.id in in_subgraph:
            continue
        if node.shape == "bare" and node.label is None and not node.classes and node.id in referenced:
            continue
        lines.append((0, render_node(node), node.order))

    children: dict[int | None, list[int]] = {}
    for i, sg in enumerate(doc.subgraphs):
        parent = sg.parent if sg.parent is not None and 0 <= sg.parent < len(doc.subgraphs) and sg.parent != i else None
        children.setdefault(parent, []).append(i)
    for top in children.get(None, []):
        _emit_subgraph(doc, top, 0, children, lines)

    for edge in doc.edges:
        lines.append((0, render_edge(edge), edge.order))

    # Resolve passthrough anchors: position -1 = right after the header.
    serial_positions: dict[int, int] = {}
    for pos, (_, _, serial) in enumerate(lines):
        if serial is not None:
            serial_positions[serial] = pos  # last position wins
    serials_sorted = sorted(serial_positions)
    inserts: dict[int, list[str]] = {}
    before_header: list[str] = []
    for p in doc.passthrough:
        if p.before_header:
            before_header.append(p.text)
            continue
        target = -1
        if p.after_statement >= 0 and serials_sorted:
            best = None
            for s in serials_sorted:
                if s <= p.after_statement:
                    best = s
                else:
                    break
            if best is not None:
                target = serial_positions[best]
        inserts.setdefault(target, []).append(p.text)

    def indent(depth: int, text: str) -> str:
        return doc.indent * (depth + 1) + text if text else ""

    out: list[str] = list(before_header)
    out.append(f"{doc.kind} {doc.direction}")
    out.extend(indent(0, t) for t in inserts.get(-1, []))
    for pos, (depth, text, _) in enumerate(lines):
        out.append(indent(depth, text))
        out.extend(indent(depth, t) for t in inserts.get(pos, []))
    return "\n".join(out) + "\n"


__all__ = ["serialize", "render_node", "render_edge", "render_label", "needs_quote"]
