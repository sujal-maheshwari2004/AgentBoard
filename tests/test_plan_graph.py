from dataclasses import dataclass, field

import pytest

from whiteboard.plan.graph import UNKNOWN_KEY, Graph, diagram_from_nodes, sync_from_diagram
from whiteboard.plan.model import Node


def mk(id, deps=(), status="todo", owner=None, title=None):
    return Node(id=id, title=title or id.removeprefix("node-").title(), depends_on=list(deps), status=status, owner=owner)


@pytest.fixture
def chain():
    return {
        "node-files": mk("node-files", status="done"),
        "node-parser": mk("node-parser", ["node-files"]),
        "node-store": mk("node-store", ["node-files", "node-parser"]),
        "node-ui": mk("node-ui"),
        "node-taken": mk("node-taken", owner="agent-x"),
    }


def test_dependents_leaves_dispatchable(chain):
    g = Graph(chain)
    assert g.dependents("node-files") == ["node-parser", "node-store"]
    assert g.dependents("node-ui") == []
    assert g.leaves() == ["node-files", "node-parser", "node-ui", "node-taken"]
    assert g.dispatchable() == ["node-parser", "node-ui"]  # files is done, taken has an owner
    chain["node-parser"].status = "done"
    assert Graph(chain).dispatchable() == ["node-store", "node-ui"]


def test_edges_and_unknown_deps(chain):
    chain["node-ui"].depends_on = ["node-missing", "node-files"]
    g = Graph(chain)
    assert g.edges() == [("node-files", "node-parser"), ("node-files", "node-store"),
                         ("node-parser", "node-store"), ("node-missing", "node-ui"), ("node-files", "node-ui")]
    assert g.unknown_deps() == {"node-ui": ["node-missing"]}
    assert "node-ui" not in g.leaves()
    assert Graph({}).unknown_deps() == {} and Graph({}).edges() == []


def test_topo_order_kahn(chain):
    order = Graph(chain).topo_order()
    assert set(order) == set(chain)
    assert order.index("node-files") < order.index("node-parser") < order.index("node-store")
    assert order[:2] == ["node-files", "node-ui"] or order[0] == "node-files"


def test_cycles_detected_and_appended_last():
    nodes = {
        "node-a": mk("node-a", ["node-b"]),
        "node-b": mk("node-b", ["node-c"]),
        "node-c": mk("node-c", ["node-a"]),
        "node-d": mk("node-d", ["node-a"]),
        "node-e": mk("node-e"),
        "node-f": mk("node-f", ["node-f"]),
    }
    g = Graph(nodes)
    cycles = g.cycles()
    assert len(cycles) == 2
    assert sorted(sorted(c) for c in cycles) == [["node-a", "node-b", "node-c"], ["node-f"]]
    order = g.topo_order()
    assert order[0] == "node-e"
    assert set(order[1:]) == {"node-a", "node-b", "node-c", "node-d", "node-f"} and len(order) == 6
    assert Graph({"node-x": mk("node-x", ["node-y"]), "node-y": mk("node-y")}).cycles() == []


def test_cycle_reported_once_from_any_entry():
    nodes = {"node-a": mk("node-a", ["node-b"]), "node-b": mk("node-b", ["node-a"]), "node-c": mk("node-c", ["node-a"])}
    assert len(Graph(nodes).cycles()) == 1


def test_large_chain_no_recursion_limit():
    nodes = {f"node-{i}": mk(f"node-{i}", [f"node-{i-1}"] if i else []) for i in range(5000)}
    nodes["node-0"].depends_on = ["node-4999"]
    assert len(Graph(nodes).cycles()) == 1
    assert len(Graph(nodes).topo_order()) == 5000


# -- sync_from_diagram ----------------------------------------------------------


def test_sync_from_diagram_reports_only_differences(chain):
    edges = [("node-files", "node-parser"), ("node-parser", "node-store"), ("node-files", "node-store"),
             ("node-ui", "node-taken"), ("node-ghost", "node-ui"), ("node-parser", "node-nope")]
    changes = sync_from_diagram(chain, edges)
    assert changes == {"node-taken": ["node-ui"], UNKNOWN_KEY: [("node-ghost", "node-ui"), ("node-parser", "node-nope")]}
    # order-only differences are not changes
    assert sync_from_diagram(chain, [("node-parser", "node-store"), ("node-files", "node-store"), ("node-files", "node-parser")]) == {}
    # removed edges clear depends_on
    assert sync_from_diagram(chain, []) == {"node-parser": [], "node-store": []}
    # duplicates collapse
    assert sync_from_diagram(chain, [("node-files", "node-parser")] * 3)["node-store"] == []


# -- diagram_from_nodes with a stub MermaidDoc ----------------------------------


@dataclass
class StubNode:
    id: str
    label: str | None = None
    shape: str = "bare"


@dataclass
class StubEdge:
    src: str
    dst: str
    label: str | None = None


@dataclass
class StubDoc:
    nodes: dict[str, StubNode] = field(default_factory=dict)
    edges: list[StubEdge] = field(default_factory=list)
    passthrough: list[str] = field(default_factory=list)

    def add_node(self, id, label=None, shape="rect"):
        n = self.nodes.get(id)
        if n is None:
            n = self.nodes[id] = StubNode(id, label, shape)
        else:
            if label is not None:
                n.label = label
            if n.shape == "bare":
                n.shape = shape
        return n

    def add_edge(self, src, dst, label=None):
        for nid in (src, dst):
            if nid not in self.nodes:
                self.nodes[nid] = StubNode(nid)
        e = StubEdge(src, dst, label)
        self.edges.append(e)
        return e

    def remove_node(self, id):
        self.nodes.pop(id, None)
        self.edges = [e for e in self.edges if id not in (e.src, e.dst)]

    def remove_edge(self, src, dst):
        for i, e in enumerate(self.edges):
            if (e.src, e.dst) == (src, dst):
                del self.edges[i]
                return True
        return False

    def rename_label(self, id, label):
        self.nodes[id].label = label

    def edge_pairs(self):
        return [(e.src, e.dst) for e in self.edges]


def test_diagram_from_nodes_syncs_existing_doc(chain):
    doc = StubDoc(passthrough=["%% keep me"])
    doc.add_node("node-ui", "UI", "round")
    doc.add_node("node-files", "Files layer")
    doc.add_node("node-obsolete", "gone")
    doc.add_edge("node-files", "node-ui", label="uses")  # not in depends_on -> removed
    doc.add_edge("node-obsolete", "node-files")
    doc.add_edge("node-files", "node-parser")  # already correct; node-parser created bare by add_edge

    out = diagram_from_nodes(chain, doc)
    assert out is doc
    assert list(doc.nodes) == ["node-ui", "node-files", "node-parser", "node-store", "node-taken"]  # order kept, obsolete gone
    assert doc.nodes["node-ui"].shape == "round" and doc.nodes["node-ui"].label == "Ui"  # label synced to title, shape kept
    assert doc.nodes["node-store"].shape == "rect" and doc.nodes["node-store"].label == "Store"
    assert doc.nodes["node-parser"].label == "Parser"
    assert sorted(doc.edge_pairs()) == sorted(Graph(chain).edges())
    assert doc.passthrough == ["%% keep me"]
    # idempotent
    before = ([(n.id, n.label, n.shape) for n in doc.nodes.values()], doc.edge_pairs())
    diagram_from_nodes(chain, doc)
    assert before == ([(n.id, n.label, n.shape) for n in doc.nodes.values()], doc.edge_pairs())


def test_diagram_from_nodes_ignores_unknown_deps_and_dedupes_edges(chain):
    chain["node-ui"].depends_on = ["node-missing"]
    doc = StubDoc()
    doc.add_edge("node-files", "node-parser")
    doc.add_edge("node-files", "node-parser")
    diagram_from_nodes(chain, doc)
    assert "node-missing" not in doc.nodes
    assert doc.edge_pairs().count(("node-files", "node-parser")) == 1


def test_diagram_from_nodes_new_doc_uses_real_mermaid_ast(chain):
    pytest.importorskip("whiteboard.mermaid.ast", reason="T2 MermaidDoc not present yet")
    from whiteboard.mermaid.ast import MermaidDoc

    doc = diagram_from_nodes(chain, None)
    assert isinstance(doc, MermaidDoc)
    assert set(doc.nodes) == set(chain)
    assert sorted(doc.edge_pairs()) == sorted(Graph(chain).edges())
