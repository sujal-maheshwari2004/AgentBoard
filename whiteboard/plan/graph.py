"""Dependency graph over plan nodes and the diagram <-> depends_on sync. See docs/CONTRACTS.md §4."""

from __future__ import annotations

from typing import TYPE_CHECKING

from whiteboard.plan.model import Node, node_ready

if TYPE_CHECKING:  # pragma: no cover - type only; never imported at runtime here
    from whiteboard.mermaid.ast import MermaidDoc

UNKNOWN_KEY = "__unknown__"


class Graph:
    """Read-only view over `{id: Node}`. Edge direction: dep -> node ("node depends on dep")."""

    def __init__(self, nodes: dict[str, Node]) -> None:
        self.nodes: dict[str, Node] = dict(nodes)

    def dependents(self, node_id: str) -> list[str]:
        """Ids of nodes that list `node_id` in depends_on (insertion order)."""
        return [n.id for n in self.nodes.values() if node_id in n.depends_on]

    def leaves(self) -> list[str]:
        """Nodes whose depends_on is empty or entirely done."""
        return [n.id for n in self.nodes.values() if node_ready(n, self.nodes)]

    def dispatchable(self) -> list[str]:
        """Status todo, no owner, and every dependency done."""
        return [
            n.id
            for n in self.nodes.values()
            if n.status == "todo" and n.owner is None and node_ready(n, self.nodes)
        ]

    def unknown_deps(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for n in self.nodes.values():
            missing = [d for d in n.depends_on if d not in self.nodes]
            if missing:
                out[n.id] = missing
        return out

    def edges(self) -> list[tuple[str, str]]:
        """(dep, node) for every depends_on entry, node order then dep order."""
        return [(dep, n.id) for n in self.nodes.values() for dep in n.depends_on]

    def _known_deps(self, node_id: str) -> list[str]:
        return [d for d in self.nodes[node_id].depends_on if d in self.nodes]

    def cycles(self) -> list[list[str]]:
        """Distinct cycles found by iterative DFS along depends_on, as node-id paths.

        `["node-a", "node-b"]` means a depends on b and b depends on a. Cycles are
        de-duplicated by rotation; each path starts at the node first reached by the DFS.
        """
        WHITE, GREY, BLACK = 0, 1, 2
        color: dict[str, int] = {nid: WHITE for nid in self.nodes}
        found: list[list[str]] = []
        seen_keys: set[tuple[str, ...]] = set()

        for start in self.nodes:
            if color[start] != WHITE:
                continue
            path: list[str] = [start]
            stack: list[tuple[str, int]] = [(start, 0)]
            color[start] = GREY
            while stack:
                nid, idx = stack[-1]
                deps = self._known_deps(nid)
                if idx < len(deps):
                    stack[-1] = (nid, idx + 1)
                    nxt = deps[idx]
                    if color[nxt] == WHITE:
                        color[nxt] = GREY
                        path.append(nxt)
                        stack.append((nxt, 0))
                    elif color[nxt] == GREY:
                        cyc = path[path.index(nxt):]
                        key = _cycle_key(cyc)
                        if key not in seen_keys:
                            seen_keys.add(key)
                            found.append(list(cyc))
                else:
                    color[nid] = BLACK
                    stack.pop()
                    path.pop()
        return found

    def topo_order(self) -> list[str]:
        """Kahn's algorithm (dependencies first). Nodes stuck in cycles are appended at the end."""
        indeg: dict[str, int] = {nid: len(self._known_deps(nid)) for nid in self.nodes}
        order: list[str] = []
        ready = [nid for nid in self.nodes if indeg[nid] == 0]
        while ready:
            nid = ready.pop(0)
            order.append(nid)
            for dep_of in self.dependents(nid):
                indeg[dep_of] -= 1
                if indeg[dep_of] == 0:
                    ready.append(dep_of)
        placed = set(order)
        order.extend(nid for nid in self.nodes if nid not in placed)
        return order


def _cycle_key(cyc: list[str]) -> tuple[str, ...]:
    """Canonical rotation so a->b->a and b->a->b compare equal."""
    if not cyc:
        return ()
    i = min(range(len(cyc)), key=lambda k: cyc[k])
    return tuple(cyc[i:] + cyc[:i])


def sync_from_diagram(
    nodes: dict[str, Node], diagram_edges: list[tuple[str, str]]
) -> dict[str, list[str]]:
    """Compute depends_on changes implied by diagram edges (src -> dst means dst depends on src).

    Returns `{node_id: new_depends_on}` for nodes whose dependency *set* differs from the
    diagram. Edges touching an id without a node file are ignored and reported under
    `"__unknown__"` as a list of `(src, dst)` pairs (key present only when non-empty).
    """
    wanted: dict[str, list[str]] = {nid: [] for nid in nodes}
    unknown: list[tuple[str, str]] = []
    for src, dst in diagram_edges:
        if src not in nodes or dst not in nodes:
            unknown.append((src, dst))
            continue
        if src not in wanted[dst]:
            wanted[dst].append(src)
    changes: dict[str, list[str]] = {}
    for nid, node in nodes.items():
        if set(node.depends_on) != set(wanted[nid]):
            changes[nid] = wanted[nid]
    if unknown:
        changes[UNKNOWN_KEY] = unknown  # type: ignore[assignment]
    return changes


def diagram_from_nodes(nodes: dict[str, Node], existing: "MermaidDoc | None") -> "MermaidDoc":
    """Make `existing` (or a fresh MermaidDoc) match `nodes`; returns the (mutated) doc.

    Keeps the doc's node order, passthrough text, shapes and edge labels; adds missing
    nodes (label = title, rect), removes nodes without a file, syncs labels to titles
    (frontmatter is authoritative), and makes the edge set equal to the `(dep, node)`
    pairs of known dependencies.
    """
    if existing is None:
        from whiteboard.mermaid.ast import MermaidDoc  # lazy: T2 builds this in parallel

        doc = MermaidDoc()
    else:
        doc = existing

    for nid in [n for n in list(doc.nodes) if n not in nodes]:
        doc.remove_node(nid)

    for nid, node in nodes.items():
        if nid not in doc.nodes:
            doc.add_node(nid, node.title, "rect")
        elif node.title and doc.nodes[nid].label != node.title:
            doc.rename_label(nid, node.title)

    wanted = {(dep, nid) for nid, node in nodes.items() for dep in node.depends_on if dep in nodes}
    seen: set[tuple[str, str]] = set()
    for src, dst in list(doc.edge_pairs()):
        if (src, dst) not in wanted or (src, dst) in seen:
            doc.remove_edge(src, dst)  # unwanted, or a duplicate of one already kept
        else:
            seen.add((src, dst))
    present = set(doc.edge_pairs())
    for nid, node in nodes.items():
        for dep in node.depends_on:
            if dep in nodes and (dep, nid) not in present:
                doc.add_edge(dep, nid)
                present.add((dep, nid))
    return doc


__all__ = ["Graph", "UNKNOWN_KEY", "diagram_from_nodes", "sync_from_diagram"]
