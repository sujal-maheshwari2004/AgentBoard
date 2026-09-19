"""AST for the Mermaid flowchart subset (docs/CONTRACTS.md §3).

Ordering model
--------------
Every *statement* (a node declaration line or one edge) carries a monotonically
increasing integer serial in its ``order`` field, shared between nodes and
edges.  A node first seen as an edge endpoint gets the serial of that edge.
``Passthrough.after_statement`` is the serial of the statement the passthrough
line followed in the source (-1 = before every statement, right after the
header).  The serializer inserts each passthrough after the *last emitted line*
whose serial is the greatest serial ``<= after_statement``; so passthroughs
follow their statement even after the doc is mutated, and ``parse`` renumbers
serials densely (0..n-1) in source order, which for canonical text equals the
statement index.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

NodeShape = Literal[
    "bare",
    "rect",
    "round",
    "stadium",
    "subroutine",
    "cylinder",
    "circle",
    "double-circle",
    "asymmetric",
    "rhombus",
    "hexagon",
    "parallelogram",
    "parallelogram-alt",
    "trapezoid",
    "trapezoid-alt",
]
EdgeStyle = Literal["solid", "dotted", "thick"]
ArrowHead = Literal["arrow", "none", "circle", "cross"]
LabelForm = Literal["pipe", "inline"]

NODE_SHAPES: tuple[str, ...] = (
    "bare",
    "rect",
    "round",
    "stadium",
    "subroutine",
    "cylinder",
    "circle",
    "double-circle",
    "asymmetric",
    "rhombus",
    "hexagon",
    "parallelogram",
    "parallelogram-alt",
    "trapezoid",
    "trapezoid-alt",
)


class MermaidError(ValueError):
    """Raised by ``parse`` on unknown syntax. ``line`` is 1-based."""

    def __init__(self, message: str, line: int = 0, text: str = "") -> None:
        self.message = message
        self.line = line
        self.text = text
        super().__init__(f"line {line}: {message}: {text!r}" if line else message)


@dataclass
class Node:
    id: str
    label: str | None = None
    shape: NodeShape = "bare"
    classes: list[str] = field(default_factory=list)
    quoted: bool = False
    order: int = 0


@dataclass
class Edge:
    src: str
    dst: str
    label: str | None = None
    style: EdgeStyle = "solid"
    head: ArrowHead = "arrow"
    length: int = 1
    label_form: LabelForm = "pipe"
    order: int = 0


@dataclass
class Subgraph:
    id: str | None = None
    title: str | None = None
    direction: str | None = None
    members: list[str] = field(default_factory=list)
    order: int = 0
    # Extension (not in CONTRACTS): index into ``MermaidDoc.subgraphs`` of the
    # enclosing subgraph for nested subgraphs; None for top-level ones.
    parent: int | None = None


@dataclass
class Passthrough:
    text: str
    after_statement: int = -1
    # Extension (not in CONTRACTS): lines that preceded the header
    # (``%%{init: ...}%%`` directives, YAML frontmatter) must stay above it.
    before_header: bool = False


@dataclass
class MermaidDoc:
    kind: Literal["flowchart", "graph"] = "flowchart"
    direction: str = "TD"
    nodes: dict[str, Node] = field(default_factory=dict)
    edges: list[Edge] = field(default_factory=list)
    subgraphs: list[Subgraph] = field(default_factory=list)
    passthrough: list[Passthrough] = field(default_factory=list)
    indent: str = "    "

    # -- serials -----------------------------------------------------------
    def next_order(self) -> int:
        """Serial for the next statement: one past the largest in use."""
        best = -1
        for n in self.nodes.values():
            if n.order > best:
                best = n.order
        for e in self.edges:
            if e.order > best:
                best = e.order
        return best + 1

    # -- mutation API ------------------------------------------------------
    def add_node(self, id: str, label: str | None = None, shape: NodeShape = "rect") -> Node:
        """Upsert with upgrade-only semantics.

        A new node is appended (first-seen order).  For an existing node a
        non-None ``label`` replaces the label and a non-bare ``shape`` fills
        in a bare shape; an existing label is never reset to None and an
        existing non-bare shape is never changed here (set ``node.shape``
        directly for that).
        """
        node = self.nodes.get(id)
        if node is None:
            node = Node(id=id, label=label, shape=shape, order=self.next_order())
            self.nodes[id] = node
            return node
        if label is not None:
            node.label = label
        if node.shape == "bare" and (shape != "bare" or node.label is not None):
            node.shape = shape if shape != "bare" else "rect"
        return node

    def add_edge(self, src: str, dst: str, label: str | None = None) -> Edge:
        """Append an edge; endpoints that do not exist become bare nodes."""
        serial = self.next_order()
        for nid in (src, dst):
            if nid not in self.nodes:
                self.nodes[nid] = Node(id=nid, order=serial)
        edge = Edge(src=src, dst=dst, label=label, order=serial)
        self.edges.append(edge)
        return edge

    def remove_node(self, id: str) -> None:
        """Delete a node together with its edges and subgraph membership."""
        self.nodes.pop(id, None)
        self.edges = [e for e in self.edges if e.src != id and e.dst != id]
        for sg in self.subgraphs:
            if id in sg.members:
                sg.members = [m for m in sg.members if m != id]

    def remove_edge(self, src: str, dst: str) -> bool:
        before = len(self.edges)
        self.edges = [e for e in self.edges if not (e.src == src and e.dst == dst)]
        return len(self.edges) != before

    def rename_label(self, id: str, label: str) -> None:
        node = self.nodes[id]
        node.label = label
        if node.shape == "bare":
            node.shape = "rect"

    def edge_pairs(self) -> list[tuple[str, str]]:
        return [(e.src, e.dst) for e in self.edges]

    # -- helpers -----------------------------------------------------------
    def subgraph_of(self, node_id: str) -> Subgraph | None:
        for sg in self.subgraphs:
            if node_id in sg.members:
                return sg
        return None

    def referenced_ids(self) -> set[str]:
        out: set[str] = set()
        for e in self.edges:
            out.add(e.src)
            out.add(e.dst)
        return out


__all__ = [
    "ArrowHead",
    "Edge",
    "EdgeStyle",
    "LabelForm",
    "MermaidDoc",
    "MermaidError",
    "NODE_SHAPES",
    "Node",
    "NodeShape",
    "Passthrough",
    "Subgraph",
]
