"""Serializer tests: determinism, idempotence, semantic round-trip (CONTRACTS §3)."""

from pathlib import Path

import pytest

from whiteboard.mermaid import Edge, MermaidDoc, Node, Passthrough, Subgraph, parse, serialize
from whiteboard.mermaid.serializer import needs_quote, render_edge, render_label, render_node

FIXTURES = Path(__file__).parent / "fixtures" / "mermaid"
ALL_FIXTURES = sorted(FIXTURES.glob("*.mmd"))


def nodes_sig(doc: MermaidDoc) -> dict[str, tuple]:
    return {n.id: (n.id, n.label, n.shape) for n in doc.nodes.values()}


def edges_sig(doc: MermaidDoc) -> list[tuple]:
    return [(e.src, e.dst, e.label, e.style, e.head, e.length, e.label_form) for e in doc.edges]


def subgraphs_sig(doc: MermaidDoc) -> list[tuple]:
    return [(s.id, s.title, s.direction, list(s.members), s.parent) for s in doc.subgraphs]


@pytest.mark.parametrize("path", ALL_FIXTURES, ids=lambda p: p.stem)
def test_idempotence(path: Path) -> None:
    x = path.read_text()
    once = serialize(parse(x))
    twice = serialize(parse(once))
    assert once == twice
    assert once.endswith("\n") and not once.endswith("\n\n")


@pytest.mark.parametrize("path", ALL_FIXTURES, ids=lambda p: p.stem)
def test_semantic_round_trip(path: Path) -> None:
    a = parse(path.read_text())
    b = parse(serialize(a))
    assert nodes_sig(a) == nodes_sig(b)
    assert edges_sig(a) == edges_sig(b)
    assert subgraphs_sig(a) == subgraphs_sig(b)
    assert {n.id: n.classes for n in a.nodes.values()} == {n.id: n.classes for n in b.nodes.values()}
    assert (a.kind, a.direction, a.indent) == (b.kind, b.direction, b.indent)
    assert [p.text for p in a.passthrough] == [p.text for p in b.passthrough]


@pytest.mark.parametrize("path", ALL_FIXTURES, ids=lambda p: p.stem)
def test_passthrough_positions_survive_round_trip(path: Path) -> None:
    """After one canonicalisation, a second parse must attach every passthrough
    to the very same statement (so serials and positions are stable)."""
    once = serialize(parse(path.read_text()))
    b = parse(once)
    assert serialize(b) == once
    c = parse(serialize(b))
    assert [(p.text, p.after_statement, p.before_header) for p in b.passthrough] == [
        (p.text, p.after_statement, p.before_header) for p in c.passthrough
    ]


CANONICAL = [
    "basic.mmd",
    "comments.mmd",
    "styles.mmd",
    "hld.mmd",
    "frontmatter.mmd",
    "two_space_indent.mmd",
]


@pytest.mark.parametrize("name", CANONICAL)
def test_canonical_fixtures_are_byte_stable(name: str) -> None:
    text = (FIXTURES / name).read_text()
    assert serialize(parse(text)) == text


def test_layout_nodes_subgraphs_edges_and_passthrough() -> None:
    src = """flowchart TD
    %% top
    A[Alpha] --> B
    %% after first edge
    subgraph S [Sub]
        C[Gamma]
    end
    B --> C
    classDef x fill:#fff
"""
    assert serialize(parse(src)) == """flowchart TD
    %% top
    A[Alpha]
    subgraph S [Sub]
        C[Gamma]
    end
    A --> B
    %% after first edge
    B --> C
    classDef x fill:#fff
"""


def test_passthrough_follows_statement_after_mutation() -> None:
    doc = parse("flowchart TD\n    A[Alpha]\n    B[Beta]\n    %% about A-B\n    A --> B\n    %% tail\n")
    doc.add_node("C", "Gamma")
    doc.add_edge("B", "C")
    # "%% about A-B" followed B[Beta] in the source, so it stays glued to B even
    # though C is appended to the node section; "%% tail" follows the edge.
    assert serialize(doc) == (
        "flowchart TD\n    A[Alpha]\n    B[Beta]\n    %% about A-B\n    C[Gamma]\n    A --> B\n    %% tail\n    B --> C\n"
    )
    doc.remove_node("B")  # passthroughs re-anchor to the nearest earlier statement
    assert serialize(doc) == "flowchart TD\n    A[Alpha]\n    %% about A-B\n    %% tail\n    C[Gamma]\n"


def test_passthrough_before_all_and_before_header() -> None:
    doc = MermaidDoc()
    doc.add_edge("A", "B")
    doc.passthrough.append(Passthrough("%% first", -1))
    doc.passthrough.append(Passthrough("%%{init: {}}%%", -1, before_header=True))
    assert serialize(doc) == "%%{init: {}}%%\nflowchart TD\n    %% first\n    A --> B\n"


def test_canonical_operators_and_lengths() -> None:
    cases = {
        ("solid", "arrow", 1): "-->",
        ("solid", "arrow", 2): "--->",
        ("solid", "none", 1): "---",
        ("solid", "none", 3): "-----",
        ("solid", "circle", 1): "--o",
        ("solid", "cross", 2): "---x",
        ("dotted", "arrow", 1): "-.->",
        ("dotted", "arrow", 2): "-..->",
        ("dotted", "none", 1): "-.-",
        ("thick", "arrow", 1): "==>",
        ("thick", "arrow", 2): "===>",
        ("thick", "none", 1): "===",
        ("thick", "cross", 1): "==x",
    }
    for (style, head, length), op in cases.items():
        e = Edge("A", "B", style=style, head=head, length=length)
        assert render_edge(e) == f"A {op} B", (style, head, length)
        assert serialize(parse(f"flowchart TD\nA {op} B\n")) == f"flowchart TD\nA {op} B\n"


def test_edge_label_forms() -> None:
    assert render_edge(Edge("A", "B", "go")) == "A -->|go| B"
    assert render_edge(Edge("A", "B", "go", label_form="inline")) == "A -- go --> B"
    assert render_edge(Edge("A", "B", "go", style="dotted", label_form="inline", length=2)) == "A -. go ..-> B"
    assert render_edge(Edge("A", "B", "go", style="thick", head="none", label_form="inline")) == "A == go === B"
    assert render_edge(Edge("A", "B", "x (y)")) == 'A -->|"x (y)"| B'
    # bidirectional input is normalised to a plain arrow
    assert serialize(parse("flowchart TD\nA <--> B\n")) == "flowchart TD\nA --> B\n"


def test_label_quoting_rules() -> None:
    assert not needs_quote("Plain label")
    for bad in ["a(b)", "a[b]", "a{b}", "a|b", 'a"b', "a<b", "a>b", "a&b", "a#b", " lead", "trail "]:
        assert needs_quote(bad), bad
    assert render_label("Plain") == "Plain"
    assert render_label("Plain", quoted=True) == '"Plain"'
    assert render_label('say "hi"') == '"say #quot;hi#quot;"'
    assert render_node(Node("A", 'say "hi"', "rect")) == 'A["say #quot;hi#quot;"]'
    # #quot; round-trips back to a double quote
    doc = parse(serialize(parse('flowchart TD\nA["say #quot;hi#quot;"]\n')))
    assert doc.nodes["A"].label == 'say "hi"'


def test_render_node_shapes_and_classes() -> None:
    assert render_node(Node("A")) == "A"
    assert render_node(Node("A", "L", "rect")) == "A[L]"
    assert render_node(Node("A", "L", "round")) == "A(L)"
    assert render_node(Node("A", "L", "stadium")) == "A([L])"
    assert render_node(Node("A", "L", "subroutine")) == "A[[L]]"
    assert render_node(Node("A", "L", "cylinder")) == "A[(L)]"
    assert render_node(Node("A", "L", "circle")) == "A((L))"
    assert render_node(Node("A", "L", "double-circle")) == "A(((L)))"
    assert render_node(Node("A", "L", "asymmetric")) == "A>L]"
    assert render_node(Node("A", "L", "rhombus")) == "A{L}"
    assert render_node(Node("A", "L", "hexagon")) == "A{{L}}"
    assert render_node(Node("A", "L", "parallelogram")) == "A[/L/]"
    assert render_node(Node("A", "L", "parallelogram-alt")) == "A[\\L\\]"
    assert render_node(Node("A", "L", "trapezoid")) == "A[/L\\]"
    assert render_node(Node("A", "L", "trapezoid-alt")) == "A[\\L/]"
    assert render_node(Node("A", "L", "rect", classes=["c1"])) == "A[L]:::c1"
    assert render_node(Node("A", None, "rect")) == "A[A]"  # shape without label prints the id


def test_bare_nodes_in_edges_are_implied() -> None:
    doc = parse("flowchart TD\n    A\n    B\n    A --> B\n    C\n    D:::cls --> E\n")
    assert serialize(doc) == "flowchart TD\n    C\n    D:::cls\n    A --> B\n    D --> E\n"


def test_subgraph_members_always_declared_inside_block() -> None:
    doc = parse("flowchart TD\n    subgraph S\n        A --> B\n    end\n    B --> C\n")
    out = serialize(doc)
    assert out == "flowchart TD\n    subgraph S\n        A\n        B\n    end\n    A --> B\n    B --> C\n"
    assert parse(out).subgraphs[0].members == ["A", "B"]


def test_subgraph_header_forms_and_nesting() -> None:
    doc = MermaidDoc()
    doc.subgraphs.append(Subgraph("s1", "Title (x)", "LR", ["a"], 0))
    doc.subgraphs.append(Subgraph(None, "Just a title", None, ["b"], 1))
    doc.subgraphs.append(Subgraph("inner", None, None, ["c"], 2, parent=0))
    for nid in "abc":
        doc.add_node(nid)
    doc.nodes["a"].shape = "bare"
    doc.nodes["b"].shape = "bare"
    doc.nodes["c"].shape = "bare"
    assert serialize(doc) == (
        "flowchart TD\n"
        '    subgraph s1 ["Title (x)"]\n'
        "        direction LR\n"
        "        a\n"
        "        subgraph inner\n"
        "            c\n"
        "        end\n"
        "    end\n"
        "    subgraph Just a title\n"
        "        b\n"
        "    end\n"
    )


def test_add_node_appends_at_end_of_node_section() -> None:
    doc = parse("flowchart TD\n    A[Alpha]\n    A --> B\n")
    doc.add_node("Z", "Zed", "round")
    assert serialize(doc) == "flowchart TD\n    A[Alpha]\n    Z(Zed)\n    A --> B\n"


def test_indent_and_kind_preserved() -> None:
    doc = parse("graph LR\n  A[x]\n  A --> B\n")
    assert serialize(doc) == "graph LR\n  A[x]\n  A --> B\n"
    doc.indent = "\t"
    assert serialize(doc) == "graph LR\n\tA[x]\n\tA --> B\n"


def test_empty_doc() -> None:
    assert serialize(MermaidDoc()) == "flowchart TD\n"
    assert serialize(MermaidDoc(kind="graph", direction="BT")) == "graph BT\n"
