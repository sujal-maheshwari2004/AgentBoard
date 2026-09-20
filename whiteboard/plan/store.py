"""PlanStore — the single owner of ``.whiteboard/`` plan state (CONTRACTS §4).

Everything the server knows about nodes, diagrams, agents and layouts lives
here. Every mutation, whether it comes from an MCP tool, a canvas op or an
external file edit, funnels through :meth:`PlanStore._transition`, which
writes the changed node files, regenerates the diagrams and ``PLAN.md``, bumps
``rev``, refreshes the last-known-good cache and returns the websocket
broadcast messages (plain dicts ``{"type": ..., "payload": ...}``).

Reconciliation rules (see CONTRACTS §0/§1):

* frontmatter ``depends_on`` is authoritative; diagrams are projections of it
  and are rewritten (only when the serialized text differs);
* a diagram box with no node file creates one (its type is the board's name
  when that name is a node type — ``hld``/``lld``/``er`` — else ``hld``;
  title = box label; status ``todo``);
* on ``load()`` diagram edges and frontmatter deps are *unioned* (the server
  may have been down while either side was edited); afterwards an edit to
  either side propagates to the other with set semantics;

Boards (CONTRACTS §0). A *board* is one of ``hld``, ``lld`` and ``er``: the
three framed regions of the canvas, backed by ``plan/<name>.md``. Node types
and board names share that vocabulary, and:

* ``home(node) = node.type`` when ``plan/<type>.md`` exists and parses, else
  ``"hld"``; regeneration adds every node to its home board and keeps nodes
  already drawn in any other diagram;
* an edge is attributed to the first board (order ``hld, lld, er`` then
  alphabetically) whose fence holds both endpoints, otherwise to
  ``home(src)``; a board's mermaid therefore only carries edges whose two
  endpoints are both drawn on it, while cross-board edges live in the node
  frontmatter and in ``PlanSnapshot.edges``;
* ``PLAN.md`` shows the *full* flowchart (every node and edge) plus one
  section per board.
"""

from __future__ import annotations

import copy
import difflib
import json
import logging
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from whiteboard.events.log import now_iso
from whiteboard.files.atomic import SelfWriteRegistry, atomic_write, read_bytes
from whiteboard.files.frontmatter import join_frontmatter, split_frontmatter
from whiteboard.mermaid import (
    MermaidDoc,
    MermaidError,
    extract_mermaid_blocks,
    parse,
    replace_mermaid_block,
    serialize,
)
from whiteboard.plan import layout as layout_mod
from whiteboard.plan import risk
from whiteboard.plan.graph import Graph, diagram_from_nodes
from whiteboard.plan.jobspec import render_plan_md
from whiteboard.plan.model import (
    AGENT_STATUSES,
    LIVE_AGENT_FIELDS,
    NODE_STATUSES,
    NODE_TYPES,
    AgentCard,
    Diagram,
    Edge,
    Node,
    PlanSnapshot,
    Skeleton,
    slugify,
    validate_agent_id,
    validate_node_id,
)
from whiteboard.plan.risk import NodeSemantics, classify_ops, describe, diff_node, is_risky, op_kind
from whiteboard.scaffold import TEMPLATES_DIR

__all__ = [
    "PlanStore", "OpsResult", "ServerMessage", "DiagramState", "BOARDS", "CACHE_VERSION",
    "CARD_WRITE_INTERVAL_S", "EXTERNAL_DIFF_MAX_CHARS",
]

log = logging.getLogger(__name__)

ServerMessage = dict[str, Any]

CACHE_VERSION = 1
DIAGRAM_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
#: The three canvas boards, in display order. Other ``plan/*.md`` diagrams sort
#: alphabetically after them.
BOARDS: tuple[str, ...] = ("hld", "lld", "er")
#: A heartbeat rewrites ``card.md`` at most this often *per agent*; in between,
#: the card is only updated in memory and broadcast (CONTRACTS §4).
CARD_WRITE_INTERVAL_S = 5.0
#: Unified diffs shipped in an ``event.external`` payload are capped here.
EXTERNAL_DIFF_MAX_CHARS = 4000
_UNSET: Any = object()


def _board_order(name: str) -> tuple[int, str]:
    """Sort key: ``hld``, ``lld``, ``er``, then everything else alphabetically."""
    return (BOARDS.index(name), "") if name in BOARDS else (len(BOARDS), name)


def _type_for_diagram(name: str) -> str:
    """The node type a box on ``plan/<name>.md`` gets: the board's own name
    when it is a node type, else ``hld``."""
    return name if name in NODE_TYPES else "hld"


@dataclass
class OpsResult:
    ok: bool
    risky: bool = False
    request_id: str | None = None
    summary: str = ""
    diff: str = ""
    affected: list[str] = field(default_factory=list)
    messages: list[ServerMessage] = field(default_factory=list)
    revert: list[dict] = field(default_factory=list)
    error: str | None = None


@dataclass
class DiagramState:
    """One ``plan/<name>.md`` file: its markdown text and the parsed flowchart."""

    name: str
    path: Path
    text: str
    doc: MermaidDoc | None
    error: str | None = None
    on_disk: bool = True

    @property
    def mermaid(self) -> str:
        return serialize(self.doc) if self.doc is not None else ""


def _msg(type_: str, payload: dict) -> ServerMessage:
    return {"type": type_, "payload": payload}


def _yaml(meta: dict) -> str:
    return yaml.safe_dump(dict(meta), sort_keys=False, default_flow_style=False, allow_unicode=True)


def _render_node(node: Node) -> str:
    return join_frontmatter(node.frontmatter(), node.body)


def _render_card(card: AgentCard) -> str:
    return join_frontmatter(card.frontmatter(), card.notes)


def _semantics(node: Node | None) -> NodeSemantics | None:
    return NodeSemantics.from_node(node) if node is not None else None


def _template(name: str, default: str) -> str:
    try:
        return (TEMPLATES_DIR / name).read_text(encoding="utf-8")
    except OSError:
        return default


class PlanStore:
    """See module docstring. All methods are synchronous except
    :meth:`apply_file_change` (async signature, sync body)."""

    def __init__(self, root: Path, *, self_writes: SelfWriteRegistry | None = None) -> None:
        self.root = Path(root).resolve()
        self.wb = self.root / ".whiteboard"
        self.plan_dir = self.wb / "plan"
        self.nodes_dir = self.plan_dir / "nodes"
        self.agents_dir = self.wb / "agents"
        self.cache_path = self.wb / ".cache" / "last_good.json"
        self.self_writes = self_writes if self_writes is not None else SelfWriteRegistry()
        self.rev = 0
        self.nodes: dict[str, Node] = {}
        self.agents: dict[str, AgentCard] = {}
        self.diagrams: dict[str, DiagramState] = {}
        self.layouts: dict[str, dict] = {}
        self.invalid: dict[str, str] = {}
        self.pending_edits: dict[str, dict] = {}
        #: Diagram proposals awaiting the owner's decision, keyed by request id
        #: (``{name, mermaid, rationale, agent_id, created_at, seq}``). Rebuilt
        #: from unresolved ``diagram_proposed`` events at load (CONTRACTS §4).
        self.pending_diagrams: dict[str, dict] = {}
        self.last_good: dict = {}
        self.last_messages: list[ServerMessage] = []
        self.writes = 0
        self._loaded = False
        # heartbeat bookkeeping: cards changed in memory but not yet on disk,
        # and when each card was last written (monotonic seconds).
        self._dirty_cards: set[str] = set()
        self._card_written_at: dict[str, float] = {}

    # ------------------------------------------------------------------ paths
    def _rel(self, path: Path) -> str:
        try:
            return Path(path).resolve().relative_to(self.root).as_posix()
        except ValueError:
            return Path(path).as_posix()

    def _node_path(self, node_id: str) -> Path:
        return self.nodes_dir / f"{node_id}.md"

    def _node_rel(self, node_id: str) -> str:
        return f".whiteboard/plan/nodes/{node_id}.md"

    def _diagram_path(self, name: str) -> Path:
        return self.plan_dir / f"{name}.md"

    def _layout_path(self, name: str) -> Path:
        return self.plan_dir / f"{name}.layout.json"

    def _agent_dir(self, agent_id: str) -> Path:
        return self.agents_dir / agent_id

    @property
    def project(self) -> str:
        return self.root.name

    # ---------------------------------------------------------------- writing
    def _write(self, path: Path, text: str, *, fsync: bool = True) -> bool:
        """Atomic write-if-changed; notes the bytes for echo suppression."""
        data = text.encode("utf-8")
        if read_bytes(path) == data:
            return False
        path.parent.mkdir(parents=True, exist_ok=True)
        self.self_writes.note(path, data)
        atomic_write(path, data, fsync=fsync)
        self.writes += 1
        return True

    def _unlink(self, path: Path) -> bool:
        try:
            path.unlink()
        except FileNotFoundError:
            return False
        self.self_writes.forget(path)
        self.writes += 1
        return True

    # ---------------------------------------------------------------- loading
    def load(self) -> PlanSnapshot:
        """Read everything under ``.whiteboard/``, reconcile, return a snapshot."""
        self.nodes_dir.mkdir(parents=True, exist_ok=True)
        self.agents_dir.mkdir(parents=True, exist_ok=True)
        self.invalid = {}
        self.last_good = self.read_last_good()

        nodes: dict[str, Node] = {}
        for path in sorted(self.nodes_dir.glob("*.md")):
            if path.name.startswith("."):
                continue
            try:
                node = self._read_node_file(path)
            except ValueError as exc:
                self._mark_invalid(path, str(exc))
                continue
            if node.id in nodes:
                self._mark_invalid(path, f"duplicate node id {node.id!r}")
                continue
            nodes[node.id] = node
        self.nodes = nodes

        previous = self.diagrams
        self.diagrams = {}
        for path in sorted(self.plan_dir.glob("*.md")):
            if path.name.startswith("."):
                continue
            state = self._read_diagram_file(path, previous.get(path.stem))
            if state is not None:
                self.diagrams[state.name] = state
        if "hld" not in self.diagrams:
            self.diagrams["hld"] = self._new_diagram_state("hld")
        # The three boards first, in canvas order, then the rest alphabetically.
        self.diagrams = {k: self.diagrams[k] for k in sorted(self.diagrams, key=_board_order)}

        self.agents = {}
        for card_path in sorted(self.agents_dir.glob("*/card.md")):
            try:
                card = self._read_agent_dir(card_path.parent)
            except ValueError as exc:
                self._mark_invalid(card_path, str(exc))
                continue
            self.agents[card.id] = card

        self.layouts = {name: layout_mod.read_layout(self._layout_path(name)) for name in self.diagrams}

        # Reconcile: boxes -> node files, union of edges and depends_on.
        new_nodes = dict(self.nodes)
        for name, state in self.diagrams.items():
            if state.doc is None:
                continue
            new_nodes, _created = self._reconcile_diagram(name, state.doc, new_nodes, mode="union")
        for problem in risk.graph_problems(new_nodes):
            log.warning("plan graph: %s", problem)
        before = self.writes
        self._transition(new_nodes, bump_rev=False)
        if self.writes > before:
            self.rev += 1
        self._loaded = True
        return self.snapshot()

    def _mark_invalid(self, path: Path, error: str) -> None:
        rel = self._rel(path)
        self.invalid[rel] = error
        log.warning("skipping invalid file %s: %s", rel, error)

    def _read_node_file(self, path: Path) -> Node:
        text = path.read_text(encoding="utf-8")
        meta, body = split_frontmatter(text)
        # join_frontmatter always emits exactly one blank separator line, so
        # leading newlines carry no information; drop them for stable equality.
        node = Node.from_frontmatter(meta, body.lstrip("\n"), self._node_rel(path.stem))
        if node.id != path.stem:
            raise ValueError(f"frontmatter id {node.id!r} does not match file name {path.stem!r}")
        return node

    def _read_diagram_file(self, path: Path, previous: DiagramState | None) -> DiagramState | None:
        """Parse ``plan/<name>.md``; None when it holds no mermaid block."""
        text = path.read_text(encoding="utf-8")
        blocks = extract_mermaid_blocks(text)
        if not blocks:
            return None
        name = path.stem
        try:
            doc = parse(blocks[0][2])
        except MermaidError as exc:
            self._mark_invalid(path, f"mermaid parse error: {exc}")
            if previous is not None and previous.doc is not None:
                return DiagramState(name, path, text, previous.doc, error=str(exc))
            return DiagramState(name, path, text, None, error=str(exc))
        return DiagramState(name, path, text, doc)

    def _new_diagram_state(self, name: str) -> DiagramState:
        path = self._diagram_path(name)
        if path.exists():
            state = self._read_diagram_file(path, None)
            if state is not None:
                return state
        text = _template(f"{name}.md", f"# {name.upper()}\n\n```mermaid\nflowchart TD\n```\n")
        blocks = extract_mermaid_blocks(text)
        doc = parse(blocks[0][2]) if blocks else MermaidDoc()
        return DiagramState(name, path, text, doc, on_disk=False)

    def _read_agent_dir(self, folder: Path) -> AgentCard:
        text = (folder / "card.md").read_text(encoding="utf-8")
        meta, body = split_frontmatter(text)
        body = body.lstrip("\n")
        plan_md = self._read_text(folder / "plan.md")
        diagrams_md = self._read_text(folder / "diagrams.md")
        if "id" not in meta or meta.get("id") in (None, ""):
            meta["id"] = folder.name
        card = AgentCard.from_frontmatter(meta, body, plan_md, diagrams_md)
        if card.id != folder.name:
            raise ValueError(f"card id {card.id!r} does not match folder {folder.name!r}")
        return card

    @staticmethod
    def _read_text(path: Path) -> str:
        data = read_bytes(path)
        return data.decode("utf-8") if data is not None else ""

    # ------------------------------------------------------- reconciliation
    def _home_diagram(self, node: Node) -> str:
        """``node.type`` when that board exists and parses, else ``hld``."""
        state = self.diagrams.get(node.type)
        return node.type if state is not None and state.doc is not None else "hld"

    def _diagram_nodes(self, name: str, doc: MermaidDoc) -> dict[str, Node]:
        """Nodes a diagram must show: those homed there plus those already drawn."""
        return {
            nid: n for nid, n in self.nodes.items() if self._home_diagram(n) == name or nid in doc.nodes
        }

    def _diagram_for(self, src: str, dst: str) -> str:
        """The first board holding both endpoints; otherwise the source's home
        board (a cross-board edge is attributed to where it starts)."""
        for name, state in self.diagrams.items():
            if state.doc is not None and src in state.doc.nodes and dst in state.doc.nodes:
                return name
        node = self.nodes.get(src)
        return self._home_diagram(node) if node is not None else "hld"

    def _reconcile_diagram(
        self, name: str, doc: MermaidDoc, nodes: dict[str, Node], *, mode: str
    ) -> tuple[dict[str, Node], list[str]]:
        """Create nodes for boxes and sync depends_on from the diagram's edges.

        ``mode="union"`` only adds edges to depends_on (load); ``mode="set"``
        makes depends_on among the diagram's boxes equal to its edges (an edit
        to the diagram is authoritative for the edges it can show).
        Returns ``(new_nodes, created_ids)``; ``nodes`` is not mutated.
        """
        nodes = dict(nodes)
        created: list[str] = []
        box_ids: list[str] = []
        for bid, box in doc.nodes.items():
            try:
                validate_node_id(bid)
            except ValueError as exc:
                log.warning("diagram %s: dropping box %r: %s", name, bid, exc)
                self.invalid[f".whiteboard/plan/{name}.md#{bid}"] = str(exc)
                continue
            box_ids.append(bid)
            if bid not in nodes:
                nodes[bid] = Node(
                    id=bid,
                    type=_type_for_diagram(name),
                    title=box.label or bid,
                    path=self._node_rel(bid),
                )
                created.append(bid)
        box_set = set(box_ids)
        wanted: dict[str, list[str]] = {bid: [] for bid in box_ids}
        for src, dst in doc.edge_pairs():
            if src in box_set and dst in box_set and src != dst and src not in wanted[dst]:
                wanted[dst].append(src)
        for bid in box_ids:
            node = nodes[bid]
            old = list(node.depends_on)
            if mode == "union":
                new = old + [s for s in wanted[bid] if s not in old]
            else:
                new = [d for d in old if d in wanted[bid] or d not in box_set]
                new += [s for s in wanted[bid] if s not in old]
            if new != old:
                nodes[bid] = node.model_copy(update={"depends_on": new})
        return nodes, created

    # -------------------------------------------------------- the core step
    def _transition(
        self,
        new_nodes: dict[str, Node],
        *,
        on_disk: frozenset[str] | set[str] = frozenset(),
        bump_rev: bool = True,
    ) -> list[ServerMessage]:
        """Move the store to ``new_nodes``: write changed node files (except
        ``on_disk`` ids, already there), regenerate diagrams and PLAN.md,
        refresh the cache and return the broadcast messages."""
        old = self.nodes
        ordered: dict[str, Node] = {nid: new_nodes[nid] for nid in old if nid in new_nodes}
        for nid, node in new_nodes.items():
            ordered.setdefault(nid, node)
        self.nodes = ordered
        writes_before = self.writes

        upserts: list[str] = []
        deletes: list[str] = []
        edge_up: list[tuple[str, str]] = []
        edge_del: list[tuple[str, str]] = []
        for nid in old:
            if nid not in ordered:
                self._unlink(self._node_path(nid))
                deletes.append(nid)
                edge_del.extend((dep, nid) for dep in old[nid].depends_on)
        for nid, node in ordered.items():
            prev = old.get(nid)
            if prev is not None and prev == node:
                continue
            if nid not in on_disk:
                self._write(self._node_path(nid), _render_node(node))
            self.invalid.pop(self._node_rel(nid), None)
            upserts.append(nid)
            old_deps = list(prev.depends_on) if prev is not None else []
            edge_up.extend((d, nid) for d in node.depends_on if d not in old_deps)
            edge_del.extend((d, nid) for d in old_deps if d not in node.depends_on)

        self._regen_diagrams()
        self._regen_plan_md()
        if deletes:
            self._gc_layouts()
        if bump_rev and (upserts or deletes or self.writes > writes_before):
            self.rev += 1
        self._save_cache()

        known = set(self.nodes) | set(old)
        msgs: list[ServerMessage] = [self._node_upsert_msg(self.nodes[nid]) for nid in upserts]
        msgs += [self._edge_delete_msg(s, d) for s, d in edge_del if s in known]
        msgs += [self._edge_upsert_msg(s, d) for s, d in edge_up if s in self.nodes]
        msgs += [_msg("plan.node.delete", {"id": nid}) for nid in deletes]
        self.last_messages = msgs
        return msgs

    def _node_upsert_msg(self, node: Node) -> ServerMessage:
        return _msg("plan.node.upsert", {"node": node.model_dump(mode="json")})

    def _edge_upsert_msg(self, src: str, dst: str) -> ServerMessage:
        edge = Edge(src=src, dst=dst, diagram=self._diagram_for(src, dst))
        return _msg("plan.edge.upsert", {"edge": edge.model_dump(mode="json")})

    def _edge_delete_msg(self, src: str, dst: str) -> ServerMessage:
        return _msg("plan.edge.delete", {"src": src, "dst": dst, "diagram": self._diagram_for(src, dst)})

    def _agent_upsert_msg(self, card: AgentCard) -> ServerMessage:
        return _msg("agent.card.upsert", {"agent": card.model_dump(mode="json")})

    def _regen_diagrams(self) -> None:
        if "hld" not in self.diagrams:
            self.diagrams = {"hld": self._new_diagram_state("hld"), **self.diagrams}
        for name, state in self.diagrams.items():
            if state.doc is None:
                continue
            state.doc = diagram_from_nodes(self._diagram_nodes(name, state.doc), state.doc)
            wanted = replace_mermaid_block(state.text, 0, serialize(state.doc))
            if wanted != state.text or not state.on_disk:
                state.text = wanted
                self._write(state.path, wanted)
                state.on_disk = True

    def _full_mermaid(self) -> str:
        """Every node and edge in one flowchart, built from a *sorted* node map
        so a reload regenerates byte-identical text (no write on load)."""
        return serialize(diagram_from_nodes(dict(sorted(self.nodes.items())), None))

    def _boards(self) -> dict[str, str]:
        return {name: s.mermaid for name, s in self.diagrams.items() if s.doc is not None}

    def _regen_plan_md(self) -> None:
        text = render_plan_md(self.nodes, self.agents, self._full_mermaid(), boards=self._boards())
        self._write(self.wb / "PLAN.md", text)

    def regenerate_plan_md(self) -> None:
        self._regen_plan_md()

    def _gc_layouts(self) -> None:
        node_ids, agent_ids = set(self.nodes), set(self.agents)
        for name, current in list(self.layouts.items()):
            cleaned = layout_mod.gc_layout(current, node_ids, agent_ids)
            if any(cleaned[k] != current.get(k) for k in layout_mod.LAYOUT_MAPS):
                self._write_layout(name, cleaned)

    def _write_layout(self, name: str, layout: dict) -> dict:
        path = self._layout_path(name)

        def writer(p: Path, data: bytes) -> None:
            p.parent.mkdir(parents=True, exist_ok=True)
            self.self_writes.note(p, data)
            atomic_write(p, data, fsync=False)
            self.writes += 1

        layout_mod.write_layout(path, layout, writer=writer)
        self.layouts[name] = layout
        return layout

    # ------------------------------------------------------- last-known-good
    def _save_cache(self) -> None:
        mtimes: dict[str, float] = {}
        paths = [self._node_path(nid) for nid in self.nodes]
        paths += [s.path for s in self.diagrams.values()]
        paths += [self._agent_dir(aid) / "card.md" for aid in self.agents]
        for p in paths:
            try:
                mtimes[self._rel(p)] = os.stat(p).st_mtime
            except OSError:
                continue
        self.last_good = {
            "version": CACHE_VERSION,
            "saved_at": now_iso(),
            "nodes": {
                nid: {
                    "node_id": nid,
                    "type": n.type,
                    "status": n.status,
                    "depends_on": sorted(n.depends_on),
                    "interfaces": list(NodeSemantics.from_node(n).interfaces),
                }
                for nid, n in self.nodes.items()
            },
            "mtimes": mtimes,
        }
        data = (json.dumps(self.last_good, indent=2, sort_keys=False) + "\n").encode("utf-8")
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write(self.cache_path, data, fsync=False)
        except OSError as exc:  # pragma: no cover - cache is best effort
            log.warning("could not write %s: %s", self.cache_path, exc)

    def read_last_good(self) -> dict:
        """The cached ``{version, saved_at, nodes, mtimes}`` or ``{}``."""
        data = read_bytes(self.cache_path)
        if data is None:
            return {}
        try:
            parsed = json.loads(data.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}
        return parsed if isinstance(parsed, dict) and parsed.get("version") == CACHE_VERSION else {}

    def last_good_semantics(self) -> dict[str, NodeSemantics]:
        out: dict[str, NodeSemantics] = {}
        for nid, rec in (self.last_good.get("nodes") or {}).items():
            try:
                out[nid] = NodeSemantics(
                    node_id=nid,
                    type=str(rec.get("type", "hld")),
                    status=str(rec.get("status", "todo")),
                    depends_on=frozenset(rec.get("depends_on") or []),
                    interfaces=tuple(str(i) for i in rec.get("interfaces") or []),
                )
            except (AttributeError, TypeError):
                continue
        return out

    # ------------------------------------------------------------- queries
    def snapshot(self) -> PlanSnapshot:
        edges: list[Edge] = []
        for node in self.nodes.values():
            for dep in node.depends_on:
                if dep in self.nodes:
                    edges.append(Edge(src=dep, dst=node.id, diagram=self._diagram_for(dep, node.id)))
        return PlanSnapshot(
            project=self.project,
            rev=self.rev,
            nodes=list(self.nodes.values()),
            edges=edges,
            agents=list(self.agents.values()),
            diagrams=[self._diagram_model(s) for s in self.diagrams.values() if s.doc is not None],
            layout={name: copy.deepcopy(lay) for name, lay in self.layouts.items()},
        )

    def _diagram_model(self, state: DiagramState) -> Diagram:
        doc = state.doc
        assert doc is not None
        return Diagram(
            name=state.name,
            direction=doc.direction,
            edges=[Edge(src=e.src, dst=e.dst, label=e.label, diagram=state.name) for e in doc.edges],
            mermaid=serialize(doc),
            path=self._rel(state.path),
        )

    def skeleton(self) -> Skeleton:
        return Skeleton(
            project=self.project,
            nodes=[
                {"id": n.id, "title": n.title, "type": n.type, "status": n.status, "depends_on": list(n.depends_on)}
                for n in self.nodes.values()
            ],
        )

    def get_node(self, id: str) -> Node | None:
        return self.nodes.get(id)

    def get_agent(self, id: str) -> AgentCard | None:
        return self.agents.get(id)

    def get_diagram(self, name: str) -> Diagram | None:
        state = self.diagrams.get(name)
        return self._diagram_model(state) if state is not None and state.doc is not None else None

    def collaboration_text(self) -> str:
        data = read_bytes(self.wb / "COLLABORATION.md")
        if data is not None:
            return data.decode("utf-8")
        return _template("COLLABORATION.md", "# COLLABORATION\n")

    def plan_md_text(self) -> str:
        return self._read_text(self.wb / "PLAN.md")

    # ---------------------------------------------------- external changes
    async def apply_file_change(self, path: str) -> list[ServerMessage]:
        """Handle one debounced watcher path. Echoes of our own writes and
        files we do not own return ``[]``; otherwise the file is reparsed, the
        store reconciled and the broadcast messages returned (plus an
        ``event.external`` message describing a non-echo edit)."""
        return self._apply_file_change(path)

    def _classify_path(self, path: Path) -> tuple[str, str] | None:
        """``(kind, id)`` for a path under ``.whiteboard``: node, diagram, agent."""
        try:
            rel = path.relative_to(self.wb)
        except ValueError:
            return None
        parts = rel.parts
        if not parts or any(p.startswith(".") for p in parts):
            return None
        if len(parts) == 3 and parts[0] == "plan" and parts[1] == "nodes" and parts[2].endswith(".md"):
            return "node", parts[2][:-3]
        if len(parts) == 2 and parts[0] == "plan" and parts[1].endswith(".md"):
            return "diagram", parts[1][:-3]
        if len(parts) == 3 and parts[0] == "agents" and parts[2] in ("card.md", "plan.md", "diagrams.md"):
            return "agent", parts[1]
        return None

    def _apply_file_change(self, path_str: str) -> list[ServerMessage]:
        path = Path(os.path.realpath(path_str))
        if path.name == "events.jsonl":
            return []
        kind_id = self._classify_path(path)
        if kind_id is None:
            return []
        kind, ident = kind_id
        data = read_bytes(path)
        if data is None:
            return self._external_delete(kind, ident, path)
        if self.self_writes.is_echo(path, data):
            return []
        if kind == "node":
            return self._external_node(ident, path)
        if kind == "diagram":
            return self._external_diagram(ident, path, data.decode("utf-8", errors="replace"))
        return self._external_agent(ident, path)

    def _external_msg(self, path: Path, summary: str, risky: bool, **extra: Any) -> ServerMessage:
        payload = {"summary": summary, "risky": risky, "path": self._rel(path)}
        payload.update(extra)
        return _msg("event.external", payload)

    def _external_delete(self, kind: str, ident: str, path: Path) -> list[ServerMessage]:
        self.self_writes.forget(path)
        self.invalid.pop(self._rel(path), None)
        if kind == "node":
            if ident not in self.nodes:
                return []
            msgs = self._delete_nodes([ident])
            msgs.append(self._external_msg(path, f"deleted {ident}", True, kind="node", ids=[ident]))
            return msgs
        if kind == "diagram":
            state = self.diagrams.get(ident)
            if state is None or ident == "hld":
                # hld is always kept; it is recreated on the next regeneration.
                if state is not None:
                    state.on_disk = False
                    self._regen_diagrams()
                    self.rev += 1
                return []
            del self.diagrams[ident]
            self.layouts.pop(ident, None)
            self.rev += 1
            self.last_messages = [self._external_msg(path, f"diagram {ident} removed", False, kind="diagram", ids=[])]
            return list(self.last_messages)
        # agent folder files
        if path.name == "card.md":
            if ident not in self.agents:
                return []
            del self.agents[ident]
            self._regen_plan_md()
            self.rev += 1
            self._save_cache()
            self.last_messages = [_msg("agent.card.delete", {"id": ident})]
            return list(self.last_messages)
        return self._external_agent(ident, path)

    def _external_node(self, node_id: str, path: Path) -> list[ServerMessage]:
        try:
            node = self._read_node_file(path)
        except ValueError as exc:
            self._mark_invalid(path, str(exc))
            return [self._external_msg(path, f"invalid node file: {exc}", False, kind="node", ids=[node_id], error=str(exc))]
        old = self.nodes.get(node.id)
        if old == node:
            return []
        new_nodes = dict(self.nodes)
        new_nodes[node.id] = node
        msgs = self._transition(new_nodes, on_disk={node.id})
        changes = diff_node(_semantics(old), _semantics(node))
        lines = [f"{node.id}: {line}" for line in describe(changes).splitlines()] if changes else []
        if old is not None and old.title != node.title:
            lines.append(f"{node.id}: renamed to {node.title!r}")
        if lines:
            msgs.append(self._external_msg(path, "\n".join(lines), is_risky(changes), kind="node", ids=[node.id]))
        self.last_messages = msgs
        return msgs

    def _external_diagram(self, name: str, path: Path, text: str) -> list[ServerMessage]:
        blocks = extract_mermaid_blocks(text)
        if not blocks:
            if name in self.diagrams and name != "hld":
                del self.diagrams[name]
                self.rev += 1
            return []
        try:
            doc = parse(blocks[0][2])
        except MermaidError as exc:
            self._mark_invalid(path, f"mermaid parse error: {exc}")
            state = self.diagrams.get(name)
            if state is not None:
                state.error = str(exc)
                state.text = text
            return [self._external_msg(path, f"mermaid parse error: {exc}", False, kind="diagram", ids=[], error=str(exc))]
        self.invalid.pop(self._rel(path), None)
        state = self.diagrams.get(name)
        if state is None:
            state = self.diagrams[name] = DiagramState(name, path, text, doc)
            self.layouts.setdefault(name, layout_mod.read_layout(self._layout_path(name)))
        else:
            state.doc, state.text, state.error, state.on_disk = doc, text, None, True
        old_nodes = self.nodes
        new_nodes, created = self._reconcile_diagram(name, doc, old_nodes, mode="set")
        for problem in risk.graph_problems(new_nodes):
            log.warning("plan graph after edit of %s: %s", self._rel(path), problem)
        msgs = self._transition(new_nodes)
        lines: list[str] = []
        risky = False
        for nid in new_nodes:
            old, new = old_nodes.get(nid), new_nodes[nid]
            if old == new:
                continue
            changes = diff_node(_semantics(old), _semantics(new))
            risky = risky or is_risky(changes)
            lines += [f"{nid}: {line}" for line in describe(changes).splitlines()]
        if lines:
            msgs.append(self._external_msg(path, "\n".join(lines), risky, kind="diagram", ids=[nid for nid in new_nodes if old_nodes.get(nid) != new_nodes[nid]]))
        self.last_messages = msgs
        return msgs

    #: ``field`` -> the file under ``agents/<id>/`` that holds it.
    _AGENT_FIELD_FILES = {"plan_md": "plan.md", "diagrams_md": "diagrams.md"}

    def agent_field_diff(self, agent_id: str, field: str, old: str, new: str) -> tuple[str, str]:
        """``(unified diff capped at EXTERNAL_DIFF_MAX_CHARS, first changed line)``
        for one of an agent's text files. The second element is a ≤120-char
        summary suitable for a push line."""
        rel = f".whiteboard/agents/{agent_id}/{self._AGENT_FIELD_FILES.get(field, field)}"
        diff = "".join(
            difflib.unified_diff(
                (old or "").splitlines(keepends=True),
                (new or "").splitlines(keepends=True),
                fromfile=f"a/{rel}",
                tofile=f"b/{rel}",
            )
        )
        if len(diff) > EXTERNAL_DIFF_MAX_CHARS:
            marker = "\n… (diff truncated)\n"
            diff = diff[: EXTERNAL_DIFF_MAX_CHARS - len(marker)] + marker
        first = ""
        for line in diff.splitlines():
            if line.startswith("+") and not line.startswith("+++") and line[1:].strip():
                first = line[1:].strip()
                break
        if not first:
            for line in diff.splitlines():
                if line.startswith("-") and not line.startswith("---") and line[1:].strip():
                    first = f"removed {line[1:].strip()}"
                    break
        return diff, first[:120]

    def _external_agent(self, agent_id: str, path: Path) -> list[ServerMessage]:
        folder = self._agent_dir(agent_id)
        if not (folder / "card.md").exists():
            return []
        try:
            card = self._read_agent_dir(folder)
        except ValueError as exc:
            self._mark_invalid(folder / "card.md", str(exc))
            return [self._external_msg(path, f"invalid agent card: {exc}", False, kind="agent", ids=[agent_id], error=str(exc))]
        self.invalid.pop(self._rel(folder / "card.md"), None)
        old = self.agents.get(agent_id)
        if old is not None and path.name != "card.md":
            # A hand-edited card.md wins, but a plan/diagram edit must not lose
            # live fields that a heartbeat has not flushed to disk yet.
            card = card.model_copy(update={k: getattr(old, k) for k in LIVE_AGENT_FIELDS})
        if old == card:
            return []
        self.agents[agent_id] = card
        self._regen_plan_md()
        self.rev += 1
        self._save_cache()
        msgs = [self._agent_upsert_msg(card)]
        field = {"plan.md": "plan_md", "diagrams.md": "diagrams_md"}.get(path.name)
        if field is not None and old is not None:
            diff, first = self.agent_field_diff(agent_id, field, getattr(old, field), getattr(card, field))
            what = "plan" if field == "plan_md" else "diagram"
            msgs.append(
                self._external_msg(
                    path,
                    f"agent {what} edited: {agent_id} — {first}",
                    False,
                    kind="agent",
                    ids=[agent_id],
                    field=field,
                    diff=diff,
                )
            )
        self.last_messages = msgs
        return list(self.last_messages)

    # ------------------------------------------------------------ canvas ops
    @staticmethod
    def _req(op: dict, key: str) -> str:
        value = op.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"op {op_kind(op)!r} is missing {key!r}")
        return value.strip()

    def _project_ops(
        self, ops: list[dict], base: dict[str, Node] | None = None
    ) -> tuple[dict[str, Node], list[str], list[tuple[str, str, str | None]]]:
        """Apply ``ops`` to a copy of the node map. Returns ``(nodes, affected,
        placements)``; raises ``ValueError`` on any invalid op or a cycle."""
        nodes = dict(self.nodes if base is None else base)
        affected: list[str] = []
        placements: list[tuple[str, str, str | None]] = []

        def touch(nid: str) -> None:
            if nid not in affected:
                affected.append(nid)

        def get(nid: str) -> Node:
            validate_node_id(nid)
            node = nodes.get(nid)
            if node is None:
                raise ValueError(f"unknown node {nid!r}")
            return node

        def set_deps(nid: str, deps: list[str]) -> None:
            nodes[nid] = nodes[nid].model_copy(update={"depends_on": deps})
            touch(nid)

        def add_edge(src: str, dst: str) -> None:
            get(src)
            node = get(dst)
            if src == dst:
                raise ValueError(f"a node cannot depend on itself ({src})")
            if src not in node.depends_on:
                set_deps(dst, list(node.depends_on) + [src])

        def remove_edge(src: str, dst: str) -> None:
            validate_node_id(src)
            node = get(dst)
            if src in node.depends_on:
                set_deps(dst, [d for d in node.depends_on if d != src])

        for op in ops:
            if not isinstance(op, dict):
                raise ValueError(f"op must be an object, got {type(op).__name__}")
            kind = op_kind(op)
            if kind == "renamed":
                node = get(self._req(op, "id"))
                label = self._req(op, "label")
                if node.title != label:
                    nodes[node.id] = node.model_copy(update={"title": label})
                    touch(node.id)
            elif kind == "node-created":
                label = str(op.get("label") or "").strip()
                nid = str(op.get("id") or "").strip() or f"node-{slugify(label)}"
                validate_node_id(nid)
                if nid in nodes:
                    raise ValueError(f"node {nid!r} already exists")
                diagram = str(op.get("diagram") or "hld")
                nodes[nid] = Node(
                    id=nid,
                    type=_type_for_diagram(diagram),
                    title=label or nid,
                    path=self._node_rel(nid),
                )
                placements.append((diagram, nid, label or None))
                touch(nid)
            elif kind == "deleted":
                nid = get(self._req(op, "id")).id
                del nodes[nid]
                touch(nid)
                for other in list(nodes.values()):
                    if nid in other.depends_on:
                        set_deps(other.id, [d for d in other.depends_on if d != nid])
            elif kind == "edge-created":
                add_edge(self._req(op, "from"), self._req(op, "to"))
            elif kind == "edge-deleted":
                remove_edge(self._req(op, "from"), self._req(op, "to"))
            elif kind == "edge-rerouted":
                remove_edge(self._req(op, "from"), self._req(op, "to"))
                add_edge(self._req(op, "new_from"), self._req(op, "new_to"))
            elif kind == "status-changed":
                node = get(self._req(op, "id"))
                status = self._req(op, "status")
                if status not in NODE_STATUSES:
                    raise ValueError(f"invalid status {status!r}; expected one of {NODE_STATUSES}")
                if node.status != status:
                    nodes[node.id] = node.model_copy(update={"status": status})
                    touch(node.id)
            else:
                raise ValueError(f"unknown op kind {kind!r}")

        self._check_cycles(nodes)
        return nodes, affected, placements

    @staticmethod
    def _check_cycles(nodes: dict[str, Node]) -> None:
        cycles = Graph(nodes).cycles()
        if cycles:
            shown = "; ".join(" -> ".join(c + c[:1]) for c in cycles)
            raise ValueError(f"dependency cycle: {shown}")

    def _describe_projection(self, new_nodes: dict[str, Node], affected: list[str]) -> tuple[str, str]:
        """``(summary, diff)`` of the projected change for the affected nodes."""
        summary: list[str] = []
        diff: list[str] = []
        for nid in affected:
            old, new = self.nodes.get(nid), new_nodes.get(nid)
            changes = diff_node(_semantics(old), _semantics(new))
            lines = describe(changes).splitlines() if changes else []
            if old is not None and new is not None and old.title != new.title:
                lines.append(f"renamed to {new.title!r}")
            summary += [f"{nid}: {line}" for line in lines] or [f"{nid}: no semantic changes"]
            rel = self._node_rel(nid)
            a = _yaml(old.frontmatter()).splitlines(keepends=True) if old is not None else []
            b = _yaml(new.frontmatter()).splitlines(keepends=True) if new is not None else []
            diff += list(
                difflib.unified_diff(
                    a, b, fromfile=f"a/{rel}" if old is not None else "/dev/null",
                    tofile=f"b/{rel}" if new is not None else "/dev/null",
                )
            )
        return "\n".join(summary), "".join(diff)

    def apply_ops(self, ops: list[dict], *, request_id: str | None = None) -> OpsResult:
        """Validate and apply canvas ops. Cosmetic-only batches commit at once;
        a batch with any risky op is parked under a fresh ``request_id`` (unless
        one is given, meaning it was already approved)."""
        ops = [dict(op) if isinstance(op, dict) else op for op in ops or []]
        try:
            new_nodes, affected, placements = self._project_ops(ops)
        except ValueError as exc:
            return OpsResult(ok=False, error=str(exc), revert=list(ops))
        _cosmetic, risky = classify_ops(ops)
        if risky and request_id is None:
            rid = uuid.uuid4().hex[:8]
            summary, diff = self._describe_projection(new_nodes, affected)
            self.pending_edits[rid] = {
                "ops": ops,
                "summary": summary,
                "diff": diff,
                "affected": list(affected),
                "created_at": now_iso(),
            }
            return OpsResult(ok=False, risky=True, request_id=rid, summary=summary, diff=diff, affected=list(affected))
        return self._commit(new_nodes, affected, placements, request_id=request_id, risky=bool(risky))

    def _commit(
        self,
        new_nodes: dict[str, Node],
        affected: list[str],
        placements: list[tuple[str, str, str | None]],
        *,
        request_id: str | None,
        risky: bool,
    ) -> OpsResult:
        summary, diff = self._describe_projection(new_nodes, affected)
        for diagram, nid, label in placements:
            state = self.diagrams.get(diagram)
            if state is not None and state.doc is not None:
                state.doc.add_node(nid, label, "rect")
        msgs = self._transition(new_nodes)
        return OpsResult(
            ok=True, risky=risky, request_id=request_id, summary=summary, diff=diff,
            affected=list(affected), messages=msgs,
        )

    def resolve_pending(self, request_id: str, approved: bool, note: str = "") -> OpsResult:
        parked = self.pending_edits.pop(request_id, None)
        if parked is None:
            return OpsResult(ok=False, error=f"unknown request_id {request_id!r}")
        ops = list(parked["ops"])
        if not approved:
            return OpsResult(
                ok=False, risky=True, request_id=request_id, summary=parked["summary"],
                affected=list(parked["affected"]), revert=ops,
            )
        try:
            new_nodes, affected, placements = self._project_ops(ops)
        except ValueError as exc:
            return OpsResult(ok=False, risky=True, request_id=request_id, error=str(exc), revert=ops)
        return self._commit(new_nodes, affected, placements, request_id=request_id, risky=True)

    # ----------------------------------------------------------- node API
    def upsert_node(
        self,
        id: str,
        title: str | None = None,
        type: str | None = None,
        status: str | None = None,
        owner: str | None = _UNSET,
        depends_on: list[str] | None = None,
        interfaces: list | None = None,
        body: str | None = None,
        diagram: str | None = None,
        **extra: Any,
    ) -> Node:
        """Create or update a node file, sync the diagrams and PLAN.md."""
        validate_node_id(id)
        existing = self.nodes.get(id)
        if type is not None and type not in NODE_TYPES:
            raise ValueError(f"invalid type {type!r}; expected one of {NODE_TYPES}")
        if status is not None and status not in NODE_STATUSES:
            raise ValueError(f"invalid status {status!r}; expected one of {NODE_STATUSES}")
        merged_extra = dict(existing.extra) if existing is not None else {}
        merged_extra.update({k: v for k, v in extra.items() if k != "path"})
        node = Node(
            id=id,
            type=type if type is not None else (existing.type if existing else "hld"),
            title=title if title is not None else (existing.title if existing else id),
            status=status if status is not None else (existing.status if existing else "todo"),
            owner=owner if owner is not _UNSET else (existing.owner if existing else None),
            depends_on=depends_on if depends_on is not None else (list(existing.depends_on) if existing else []),
            interfaces=interfaces if interfaces is not None else (list(existing.interfaces) if existing else []),
            body=body if body is not None else (existing.body if existing else ""),
            extra=merged_extra,
            path=self._node_rel(id),
        )
        if id in node.depends_on:
            raise ValueError(f"a node cannot depend on itself ({id})")
        new_nodes = dict(self.nodes)
        new_nodes[id] = node
        self._check_cycles(new_nodes)
        unknown = [d for d in node.depends_on if d not in new_nodes]
        if unknown:
            log.warning("%s depends on unknown node(s): %s", id, ", ".join(unknown))
        if diagram:
            state = self.diagrams.get(diagram)
            if state is not None and state.doc is not None:
                state.doc.add_node(id, node.title, "rect")
        self._transition(new_nodes)
        return self.nodes[id]

    def set_node_status(self, id: str, status: str, owner: str | None = None) -> Node:
        node = self.nodes.get(id)
        if node is None:
            raise ValueError(f"unknown node {id!r}")
        if status not in NODE_STATUSES:
            raise ValueError(f"invalid status {status!r}; expected one of {NODE_STATUSES}")
        update: dict[str, Any] = {"status": status}
        if owner is not None:
            update["owner"] = validate_agent_id(owner) if owner != "" else None
        new_nodes = dict(self.nodes)
        new_nodes[id] = node.model_copy(update=update)
        self._transition(new_nodes)
        return self.nodes[id]

    def delete_node(self, id: str) -> None:
        if id not in self.nodes:
            raise ValueError(f"unknown node {id!r}")
        self._delete_nodes([id])

    def _delete_nodes(self, ids: list[str]) -> list[ServerMessage]:
        new_nodes = {nid: n for nid, n in self.nodes.items() if nid not in ids}
        for nid, node in list(new_nodes.items()):
            if any(d in ids for d in node.depends_on):
                new_nodes[nid] = node.model_copy(update={"depends_on": [d for d in node.depends_on if d not in ids]})
        return self._transition(new_nodes)

    # -------------------------------------------------------- diagram API
    def validate_diagram(self, name: str, mermaid: str) -> MermaidDoc:
        """Parse and check ``mermaid`` as board ``name`` **writing nothing**:
        the diagram name, the mermaid itself, every box id and the cycle-freeness
        of the depends_on graph it implies. Raises ``ValueError`` with a message
        meant for the caller; returns the parsed document."""
        if not DIAGRAM_NAME_RE.match(name or ""):
            raise ValueError(f"invalid diagram name {name!r}")
        try:
            doc = parse(mermaid)
        except MermaidError as exc:
            raise ValueError(f"invalid mermaid: {exc}") from exc
        for bid in doc.nodes:
            try:
                validate_node_id(bid)
            except ValueError as exc:
                raise ValueError(f"diagram box {bid!r} is not a valid node id: {exc}") from exc
        new_nodes, _created = self._reconcile_diagram(name, doc, self.nodes, mode="set")
        self._check_cycles(new_nodes)
        return doc

    def diagram_diff(self, name: str, mermaid: str) -> dict:
        """What writing ``mermaid`` to board ``name`` would change, as
        ``{nodes_added, nodes_removed, edges_added, edges_removed}``. Validates
        first (so an invalid proposal raises here) and writes nothing."""
        doc = self.validate_diagram(name, mermaid)
        state = self.diagrams.get(name)
        current = state.doc if state is not None else None
        cur_boxes = dict(current.nodes) if current is not None else {}
        cur_edges = {(e.src, e.dst): e.label for e in (current.edges if current is not None else [])}
        new_edges = {(e.src, e.dst): e.label for e in doc.edges}
        return {
            "nodes_added": [
                {"id": bid, "label": box.label or bid}
                for bid, box in doc.nodes.items()
                if bid not in cur_boxes
            ],
            "nodes_removed": [bid for bid in cur_boxes if bid not in doc.nodes],
            "edges_added": [
                {"src": src, "dst": dst, "label": label}
                for (src, dst), label in new_edges.items()
                if (src, dst) not in cur_edges
            ],
            "edges_removed": [
                {"src": src, "dst": dst, "label": label}
                for (src, dst), label in cur_edges.items()
                if (src, dst) not in new_edges
            ],
        }

    def write_diagram(self, name: str, mermaid: str) -> Diagram:
        """Validate ``mermaid``, write ``plan/<name>.md`` with the block replaced
        and sync depends_on from its edges (creating missing nodes)."""
        doc = self.validate_diagram(name, mermaid)
        new_nodes, _created = self._reconcile_diagram(name, doc, self.nodes, mode="set")
        state = self.diagrams.get(name)
        if state is None:
            state = self._new_diagram_state(name)
            state.doc = doc
            self.diagrams[name] = state
            self.layouts.setdefault(name, layout_mod.read_layout(self._layout_path(name)))
        else:
            state.doc = doc
            state.error = None
            self.invalid.pop(self._rel(state.path), None)
        state.text = replace_mermaid_block(state.text, 0, serialize(doc))
        state.on_disk = False  # force the regen write even if the text matches
        self._transition(new_nodes)
        return self._diagram_model(self.diagrams[name])

    # ---------------------------------------------------------- agent API
    def _default_plan_md(self, agent_id: str, node_id: str | None) -> str:
        text = _template("agent-plan.md", "# Job spec: node-example\n")
        return text.replace("node-example", node_id or "(unassigned)")

    @staticmethod
    def _default_diagrams_md(agent_id: str, node_id: str | None) -> str:
        """A real (empty) fence, so ``write_agent_diagram`` and the canvas both
        have a block to replace from the first write on."""
        target = node_id or "this agent's node"
        return (
            f"# Diagrams: {agent_id}\n"
            "\n"
            f"The LLD for {f'`{node_id}`' if node_id else 'this agent'}: one box per module, class "
            "or table. Refresh it with `write_agent_diagram` before you report `done`.\n"
            "\n"
            "```mermaid\n"
            "flowchart TD\n"
            f"    %% components of {target}; box ids are free-form\n"
            "```\n"
        )

    def _write_agent(self, card: AgentCard) -> None:
        folder = self._agent_dir(card.id)
        self._write(folder / "card.md", _render_card(card))
        self._write(folder / "plan.md", card.plan_md)
        self._write(folder / "diagrams.md", card.diagrams_md)

    def _commit_agent(self, card: AgentCard) -> AgentCard:
        self._write_agent(card)
        self.agents[card.id] = card
        self._dirty_cards.discard(card.id)
        self._card_written_at[card.id] = time.monotonic()
        self._regen_plan_md()
        self.rev += 1
        self._save_cache()
        self.last_messages = [self._agent_upsert_msg(card)]
        return card

    def upsert_agent(
        self,
        id: str,
        assigned_node: str | None,
        status: str | None = None,
        plan_md: str | None = None,
        diagrams_md: str | None = None,
        claude_agent_ref: str | None = None,
        notes: str | None = None,
    ) -> AgentCard:
        """Create or update ``agents/<id>/{card,plan,diagrams}.md``. ``None``
        keeps the existing value (or the template default for a new agent)."""
        validate_agent_id(id)
        existing = self.agents.get(id)
        if assigned_node is not None:
            validate_node_id(assigned_node)
            if assigned_node not in self.nodes:
                raise ValueError(f"unknown node {assigned_node!r}")
        elif existing is not None:
            assigned_node = existing.assigned_node
        if status is not None and status not in AGENT_STATUSES:
            raise ValueError(f"invalid status {status!r}; expected one of {AGENT_STATUSES}")
        node = self.nodes.get(assigned_node) if assigned_node else None
        ready = list(existing.ready_deps) if existing is not None else []
        if node is not None:
            ready = [d for d in ready if d in node.depends_on]
            ready += [d for d in node.depends_on if d not in ready and d in self.nodes and self.nodes[d].status == "done"]
        card = AgentCard(
            id=id,
            assigned_node=assigned_node,
            status=status if status is not None else (existing.status if existing else "idle"),
            claude_agent_ref=claude_agent_ref if claude_agent_ref is not None else (existing.claude_agent_ref if existing else None),
            ready_deps=ready,
            spawned_at=existing.spawned_at if existing else None,
            # live monitoring fields are carried over: a plan or diagram
            # rewrite must never drop a heartbeat.
            **({k: getattr(existing, k) for k in LIVE_AGENT_FIELDS} if existing else {}),
            notes=notes if notes is not None else (existing.notes if existing else "Optional notes.\n"),
            plan_md=plan_md if plan_md is not None else (existing.plan_md if existing and existing.plan_md else self._default_plan_md(id, assigned_node)),
            diagrams_md=diagrams_md if diagrams_md is not None else (existing.diagrams_md if existing and existing.diagrams_md else self._default_diagrams_md(id, assigned_node)),
            extra=dict(existing.extra) if existing else {},
        )
        return self._commit_agent(card)

    def write_agent_diagram(self, id: str, mermaid: str) -> AgentCard:
        """Replace the first mermaid fence of ``agents/<id>/diagrams.md`` (or
        append one). Box ids are free-form — this is the agent's own LLD, not a
        plan board. Raises ``ValueError`` on invalid mermaid."""
        card = self.agents.get(id)
        if card is None:
            raise ValueError(f"unknown agent {id!r}")
        try:
            parse(mermaid)
        except MermaidError as exc:
            raise ValueError(f"invalid mermaid: {exc}") from exc
        text = card.diagrams_md or self._default_diagrams_md(id, card.assigned_node)
        return self.upsert_agent(id, None, diagrams_md=replace_mermaid_block(text, 0, mermaid))

    # ------------------------------------------------- live monitoring
    def touch_agent(
        self,
        id: str,
        *,
        activity: str | None = None,
        progress: float | None = None,
        metrics: dict | None = None,
        files_touched: list[str] | None = None,
        finished: bool = False,
        now: str | None = None,
    ) -> AgentCard:
        """The lightweight heartbeat path (CONTRACTS §4): update the card in
        memory, broadcast it, and write ``card.md`` at most once every
        :data:`CARD_WRITE_INTERVAL_S` per agent. It never bumps ``rev``,
        regenerates ``PLAN.md`` or writes the cache."""
        card = self.agents.get(id)
        if card is None:
            raise ValueError(f"unknown agent {id!r}")
        ts = now or now_iso()
        merged = dict(card.metrics)
        merged.update(metrics or {})
        if files_touched:
            files = list(merged.get("files_touched") or [])
            files += [f for f in (str(x) for x in files_touched) if f and f not in files]
            merged["files_touched"] = files
        update: dict[str, Any] = {"heartbeat_at": ts, "metrics": merged}
        if activity is not None:
            update["activity"] = str(activity)
        if progress is not None:
            update["progress"] = min(1.0, max(0.0, float(progress)))
        if finished:
            update["finished_at"] = ts
        new_card = AgentCard.model_validate({**card.model_dump(), **update})
        self.agents[id] = new_card
        self._dirty_cards.add(id)
        self._maybe_write_card(id)
        self.last_messages = [self._agent_upsert_msg(new_card)]
        return new_card

    def _maybe_write_card(self, id: str, *, force: bool = False) -> bool:
        """Write ``agents/<id>/card.md`` when it is due; returns whether it was."""
        card = self.agents.get(id)
        if card is None:
            self._dirty_cards.discard(id)
            return False
        last = self._card_written_at.get(id)
        if not force and last is not None and (time.monotonic() - last) < CARD_WRITE_INTERVAL_S:
            return False
        self._write(self._agent_dir(id) / "card.md", _render_card(card), fsync=False)
        self._card_written_at[id] = time.monotonic()
        self._dirty_cards.discard(id)
        return True

    def flush_dirty_cards(self, force: bool = False) -> list[str]:
        """Write the cards touched since the last flush; returns the ids written."""
        return [aid for aid in sorted(self._dirty_cards) if self._maybe_write_card(aid, force=force)]

    def finalize_agent(
        self,
        id: str,
        *,
        tokens_in: int | None = None,
        tokens_out: int | None = None,
        cost_usd: float | None = None,
        duration_s: float | None = None,
    ) -> AgentCard:
        """The end of a run: merge the final metrics, stamp ``finished_at`` and
        commit the card in full (one ``rev`` bump, PLAN.md regenerated)."""
        card = self.agents.get(id)
        if card is None:
            raise ValueError(f"unknown agent {id!r}")
        metrics = dict(card.metrics)
        for key, value in (
            ("tokens_in", tokens_in), ("tokens_out", tokens_out),
            ("cost_usd", cost_usd), ("elapsed_s", duration_s),
        ):
            if value is not None:
                metrics[key] = value
        ts = now_iso()
        final = AgentCard.model_validate({
            **card.model_dump(), "metrics": metrics, "finished_at": ts, "heartbeat_at": ts,
        })
        return self._commit_agent(final)

    def set_agent_status(self, id: str, status: str) -> AgentCard:
        card = self.agents.get(id)
        if card is None:
            raise ValueError(f"unknown agent {id!r}")
        if status not in AGENT_STATUSES:
            raise ValueError(f"invalid status {status!r}; expected one of {AGENT_STATUSES}")
        return self._commit_agent(card.model_copy(update={"status": status}))

    def set_agent_ref(self, id: str, claude_agent_ref: str | None, spawned_at: str | None = None) -> AgentCard:
        """Record the SendMessage handle the root uses for this agent."""
        card = self.agents.get(id)
        if card is None:
            raise ValueError(f"unknown agent {id!r}")
        update: dict[str, Any] = {"claude_agent_ref": claude_agent_ref}
        if spawned_at is not None:
            update["spawned_at"] = spawned_at
        return self._commit_agent(card.model_copy(update=update))

    def mark_ready_deps(self, done_node_id: str) -> list[AgentCard]:
        """Add ``done_node_id`` to ``ready_deps`` of every card whose assigned
        node depends on it; returns the cards that changed."""
        changed: list[AgentCard] = []
        for card in list(self.agents.values()):
            node = self.nodes.get(card.assigned_node) if card.assigned_node else None
            if node is None or done_node_id not in node.depends_on or done_node_id in card.ready_deps:
                continue
            new_card = card.model_copy(update={"ready_deps": list(card.ready_deps) + [done_node_id]})
            self._write_agent(new_card)
            self.agents[card.id] = new_card
            changed.append(new_card)
        if changed:
            self._regen_plan_md()
            self.rev += 1
            self._save_cache()
            self.last_messages = [self._agent_upsert_msg(c) for c in changed]
        return changed

    def delete_agent(self, id: str) -> None:
        if id not in self.agents:
            raise ValueError(f"unknown agent {id!r}")
        folder = self._agent_dir(id)
        for name in ("card.md", "plan.md", "diagrams.md"):
            self._unlink(folder / name)
        try:
            folder.rmdir()
        except OSError:
            pass
        del self.agents[id]
        self._regen_plan_md()
        self._gc_layouts()
        self.rev += 1
        self._save_cache()
        self.last_messages = [_msg("agent.card.delete", {"id": id})]

    # --------------------------------------------------------- layout API
    def save_layout(self, diagram: str, patch: dict) -> dict:
        """Merge ``patch`` into the diagram's sidecar, drop entries for ids that
        no longer exist, write it and return the new layout."""
        if not DIAGRAM_NAME_RE.match(diagram or ""):
            raise ValueError(f"invalid diagram name {diagram!r}")
        current = self.layouts.get(diagram) or layout_mod.read_layout(self._layout_path(diagram))
        merged = layout_mod.merge_layout(current, patch or {})
        cleaned = layout_mod.gc_layout(merged, set(self.nodes), set(self.agents))
        return self._write_layout(diagram, cleaned)
