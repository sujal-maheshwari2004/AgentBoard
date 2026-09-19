"""Parser and AST tests for whiteboard.mermaid (CONTRACTS §3)."""

from pathlib import Path

import pytest

from whiteboard.mermaid import MermaidDoc, MermaidError, Node, parse
from whiteboard.mermaid.parser import split_statements

FIXTURES = Path(__file__).parent / "fixtures" / "mermaid"
ALL_FIXTURES = sorted(FIXTURES.glob("*.mmd"))


def load(name: str) -> MermaidDoc:
    return parse((FIXTURES / name).read_text())


# ---------------------------------------------------------------- fixtures --


def test_fixture_corpus_size() -> None:
    assert len(ALL_FIXTURES) >= 12


@pytest.mark.parametrize("path", ALL_FIXTURES, ids=lambda p: p.stem)
def test_every_fixture_parses(path: Path) -> None:
    doc = parse(path.read_text())
    assert doc.kind in ("flowchart", "graph")
    assert doc.direction in ("TB", "TD", "BT", "LR", "RL")
    assert doc.nodes


# (fixture, node count, edge count, subgraph count)
COUNTS = [
    ("basic.mmd", 3, 2, 0),
    ("shapes.mmd", 15, 11, 0),
    ("links.mmd", 21, 20, 0),
    ("labels.mmd", 15, 14, 0),
    ("chains_groups.mmd", 21, 16, 0),
    ("subgraphs.mmd", 8, 6, 4),
    ("nested_subgraphs.mmd", 4, 3, 2),
    ("comments.mmd", 3, 2, 0),
    ("styles.mmd", 3, 2, 0),
    ("quoted.mmd", 7, 6, 0),
    ("classes.mmd", 4, 3, 0),
    ("semicolons.mmd", 6, 4, 0),
    ("hld.mmd", 9, 10, 0),
    ("graph_lr.mmd", 4, 3, 0),
    ("bare_nodes.mmd", 5, 1, 0),
    ("init_directive.mmd", 2, 1, 0),
    ("frontmatter.mmd", 2, 1, 0),
    ("two_space_indent.mmd", 2, 1, 0),
    ("real_world.mmd", 10, 12, 1),
]


@pytest.mark.parametrize("name,nodes,edges,subgraphs", COUNTS, ids=[c[0] for c in COUNTS])
def test_fixture_counts(name: str, nodes: int, edges: int, subgraphs: int) -> None:
    doc = load(name)
    assert len(doc.nodes) == nodes
    assert len(doc.edges) == edges
    assert len(doc.subgraphs) == subgraphs


def test_basic_nodes_and_edges() -> None:
    doc = load("basic.mmd")
    assert doc.kind == "flowchart" and doc.direction == "TD"
    assert list(doc.nodes) == ["A", "B", "C"]
    assert doc.nodes["A"].label == "Start" and doc.nodes["A"].shape == "rect"
    assert doc.edge_pairs() == [("A", "B"), ("B", "C")]
    assert [n.order for n in doc.nodes.values()] == [0, 1, 2]
    assert [e.order for e in doc.edges] == [3, 4]


def test_every_shape() -> None:
    doc = load("shapes.mmd")
    expected = {
        "n_bare": ("bare", None),
        "n_rect": ("rect", "Rectangle"),
        "n_round": ("round", "Round edges"),
        "n_stadium": ("stadium", "Stadium"),
        "n_sub": ("subroutine", "Subroutine"),
        "n_cyl": ("cylinder", "Cylinder"),
        "n_circle": ("circle", "Circle"),
        "n_dcircle": ("double-circle", "Double circle"),
        "n_asym": ("asymmetric", "Asymmetric"),
        "n_rhombus": ("rhombus", "Rhombus"),
        "n_hex": ("hexagon", "Hexagon"),
        "n_para": ("parallelogram", "Parallelogram"),
        "n_para_alt": ("parallelogram-alt", "Parallelogram alt"),
        "n_trap": ("trapezoid", "Trapezoid"),
        "n_trap_alt": ("trapezoid-alt", "Trapezoid alt"),
    }
    assert {k: (n.shape, n.label) for k, n in doc.nodes.items()} == expected
    # bare reference in an edge never downgrades an earlier declaration
    assert doc.nodes["n_rect"].shape == "rect"


def test_every_link() -> None:
    doc = load("links.mmd")
    got = [(e.style, e.head, e.length) for e in doc.edges]
    assert got == [
        ("solid", "arrow", 1),
        ("solid", "none", 1),
        ("dotted", "arrow", 1),
        ("dotted", "none", 1),
        ("thick", "arrow", 1),
        ("thick", "none", 1),
        ("solid", "circle", 1),
        ("solid", "cross", 1),
        ("solid", "arrow", 1),  # <--> treated as -->
        ("solid", "arrow", 2),
        ("solid", "arrow", 3),
        ("dotted", "arrow", 2),
        ("thick", "arrow", 2),
        ("solid", "none", 2),
        ("thick", "none", 2),
        ("dotted", "none", 2),
        ("solid", "circle", 2),
        ("solid", "cross", 2),
        ("dotted", "arrow", 1),  # <-.->
        ("thick", "arrow", 1),  # <==>
    ]
    assert all(e.label is None for e in doc.edges)


def test_labels_both_forms() -> None:
    doc = load("labels.mmd")
    got = [(e.label, e.label_form, e.style, e.head) for e in doc.edges]
    assert got == [
        ("pipe label", "pipe", "solid", "arrow"),
        ("inline label", "inline", "solid", "arrow"),
        ("dotted inline", "inline", "dotted", "arrow"),
        ("thick inline", "inline", "thick", "arrow"),
        ("solid none pipe", "pipe", "solid", "none"),
        ("solid none inline", "inline", "solid", "none"),
        ("dotted pipe", "pipe", "dotted", "none"),
        ("thick pipe", "pipe", "thick", "arrow"),
        ("circle head", "inline", "solid", "circle"),
        ("cross head", "inline", "solid", "cross"),
        ("quoted (pipe)", "pipe", "solid", "arrow"),
        ("quoted [inline]", "inline", "solid", "arrow"),
        ("padded", "pipe", "solid", "arrow"),
        ("two words", "pipe", "solid", "arrow"),
    ]
    assert doc.edges[-1].dst == "O"


def test_chains_and_groups_cross_product() -> None:
    doc = load("chains_groups.mmd")
    pairs = doc.edge_pairs()
    assert pairs[:3] == [("A", "B"), ("B", "C"), ("C", "D")]
    assert pairs[3:5] == [("E", "G"), ("F", "G")]
    assert pairs[5:7] == [("H", "I"), ("H", "J")]
    assert pairs[7:11] == [("K", "M"), ("K", "N"), ("L", "M"), ("L", "N")]
    assert pairs[11:13] == [("O", "Q"), ("P", "Q")]
    assert doc.nodes["O"].label == "Labelled" and doc.nodes["P"].shape == "round"
    assert doc.nodes["Q"].shape == "rhombus"
    # chain with mixed label forms
    tail = doc.edges[13:]
    assert [(e.src, e.dst, e.label, e.label_form) for e in tail] == [
        ("R", "S", None, "pipe"),
        ("S", "T", "then", "pipe"),
        ("T", "U", "and", "inline"),
    ]
    # nodes on one line share the serial of the line's first edge; edges get their own
    assert doc.nodes["A"].order == doc.nodes["D"].order == doc.edges[0].order
    assert [e.order for e in doc.edges] == list(range(len(doc.edges)))


def test_subgraphs_with_direction_and_titles() -> None:
    doc = load("subgraphs.mmd")
    sgs = [(s.id, s.title, s.direction, s.members) for s in doc.subgraphs]
    assert sgs == [
        ("one", "Group One", "LR", ["A1", "A2", "A3"]),
        ("two", None, None, ["B1", "B2"]),
        (None, "Third group", None, ["C1"]),
        ("four", "Quoted (title)", None, ["D1"]),
    ]
    assert all(s.parent is None for s in doc.subgraphs)
    assert doc.subgraph_of("Outside") is None
    # membership is first-wins: edges outside a subgraph do not move nodes
    assert doc.subgraph_of("A1").id == "one"
    assert doc.nodes["A1"].label == "First"


def test_nested_subgraphs() -> None:
    doc = load("nested_subgraphs.mmd")
    outer, inner = doc.subgraphs
    assert outer.id == "outer" and outer.parent is None and outer.members == ["O1"]
    assert inner.id == "inner" and inner.parent == 0 and inner.members == ["I1", "I2"]
    assert inner.direction == "RL"
    assert doc.subgraph_of("X") is None


def test_comments_are_passthrough_with_positions() -> None:
    doc = load("comments.mmd")
    pts = [(p.text, p.after_statement, p.before_header) for p in doc.passthrough]
    assert pts == [
        ("%% leading comment before the header", -1, True),
        ("%% after header, before any statement", -1, False),
        ("%% between node declarations", 0, False),
        ("%% before the edges", 2, False),
        ("%% between edges", 3, False),
        ("%% trailing comment", 4, False),
    ]


def test_style_lines_are_passthrough() -> None:
    doc = load("styles.mmd")
    texts = [p.text for p in doc.passthrough]
    assert texts == [
        "classDef hot fill:#f96,stroke:#333",
        "classDef cold fill:#69f",
        "class B,C cold",
        "style C fill:#bbf,stroke:#f66,stroke-width:2px",
        "linkStyle 0 stroke:#ff3,stroke-width:4px",
        'click A "https://example.com" "Open"',
        'click B callback "Tooltip"',
    ]
    assert [p.after_statement for p in doc.passthrough] == [2, 2, 2, 3, 4, 4, 4]
    assert doc.nodes["A"].classes == ["hot"]
    assert doc.nodes["B"].classes == []


def test_quoted_labels() -> None:
    doc = load("quoted.mmd")
    n = doc.nodes
    assert (n["A"].label, n["A"].quoted, n["A"].shape) == ("Service (v2)", True, "rect")
    assert n["B"].label == "Queue [primary]"
    assert n["C"].label == 'He said "hello"' and n["C"].shape == "round"
    assert n["D"].label == "Decision? (yes/no)" and n["D"].shape == "rhombus"
    assert (n["E"].label, n["E"].quoted) == ("Plain quoted", True)
    assert (n["F"].label, n["F"].quoted) == ("Unquoted plain", False)
    assert n["G"].label == "Stadium & co" and n["G"].shape == "stadium"
    assert doc.edges[3].label == "yes (really)"
    assert doc.edges[5].label == "inline (quoted)" and doc.edges[5].label_form == "inline"


def test_class_shorthand() -> None:
    doc = load("classes.mmd")
    assert doc.nodes["A"].classes == ["primary"]
    assert doc.nodes["B"].classes == ["secondary"] and doc.nodes["B"].shape == "bare"
    assert doc.nodes["C"].classes == ["primary"]
    assert doc.nodes["D"].classes == ["tertiary"]
    assert doc.edge_pairs() == [("A", "B"), ("B", "C"), ("C", "D")]


def test_semicolon_separated_statements() -> None:
    doc = load("semicolons.mmd")
    assert doc.kind == "graph" and doc.direction == "LR"
    assert list(doc.nodes) == ["A", "B", "C", "D", "E", "F"]
    assert doc.nodes["C"].label == "Gamma"
    assert doc.nodes["D"].label == "semi; inside"
    assert doc.edges[-1].label == "a; b"
    assert doc.edge_pairs() == [("A", "B"), ("B", "C"), ("D", "E"), ("E", "F")]


def test_hld_with_node_ids() -> None:
    doc = load("hld.mmd")
    assert all(k.startswith("node-") for k in doc.nodes)
    assert doc.nodes["node-server"].label == "Server + websocket"
    assert ("node-files", "node-parser") in doc.edge_pairs()
    assert len({e.dst for e in doc.edges if e.src == "node-server"}) == 3


def test_graph_lr_header() -> None:
    doc = load("graph_lr.mmd")
    assert doc.kind == "graph" and doc.direction == "LR"
    assert doc.nodes["C"].shape == "cylinder" and doc.nodes["D"].shape == "hexagon"
    assert doc.edges[2].style == "dotted" and doc.edges[2].label == "cache miss"


def test_bare_nodes_and_repeat_declaration() -> None:
    doc = load("bare_nodes.mmd")
    assert list(doc.nodes) == ["Lonely", "Another", "A", "B", "C"]
    assert doc.nodes["Lonely"].order == 0  # repeated bare mention keeps first serial


def test_init_directive_and_frontmatter_stay_before_header() -> None:
    doc = load("init_directive.mmd")
    assert len(doc.passthrough) == 1
    assert doc.passthrough[0].before_header is True
    assert doc.passthrough[0].text.startswith("%%{init")
    doc = load("frontmatter.mmd")
    assert [p.text for p in doc.passthrough] == ["---", "title: Config frontmatter", "config:", "  theme: forest", "---"]
    assert all(p.before_header for p in doc.passthrough)
    assert doc.kind == "flowchart" and doc.direction == "LR"


def test_indent_detection() -> None:
    assert load("two_space_indent.mmd").indent == "  "
    assert load("basic.mmd").indent == "    "
    assert parse("flowchart TD\nA --> B\n").indent == ""
    assert MermaidDoc().indent == "    "


def test_real_world_membership_and_groups() -> None:
    doc = load("real_world.mmd")
    (sg,) = doc.subgraphs
    assert sg.members == ["q", "w1", "w2"] and sg.direction == "LR"
    assert doc.subgraph_of("api1") is None
    assert doc.edges[0].label == "HTTP"
    assert [e.src for e in doc.edges if e.dst == "cache"] == ["api1", "api2"]
    assert doc.nodes["audit"].shape == "parallelogram"
    assert [p.text for p in doc.passthrough][0] == "%% Request path"


# ------------------------------------------------------------ edge cases --


def test_no_spaces_around_links() -> None:
    doc = parse("flowchart TD\nA-->B-.->C==>D---E\nfoo-bar-->baz.qux\n")
    assert doc.edge_pairs() == [("A", "B"), ("B", "C"), ("C", "D"), ("D", "E"), ("foo-bar", "baz.qux")]
    assert [e.style for e in doc.edges[:4]] == ["solid", "dotted", "thick", "solid"]


def test_header_without_direction_defaults_to_td() -> None:
    doc = parse("graph\nA --> B\n")
    assert doc.kind == "graph" and doc.direction == "TD"


def test_crlf_and_blank_lines() -> None:
    doc = parse("flowchart TD\r\n    A[x]\r\n\r\n    A --> B\r\n\r\n")
    assert doc.nodes["A"].label == "x"
    assert [(p.text, p.after_statement) for p in doc.passthrough] == [("", 0)]


def test_later_shaped_declaration_updates_node() -> None:
    doc = parse("flowchart TD\nA --> B\nA[Now labelled]\nB(Round)\n")
    assert doc.nodes["A"].label == "Now labelled" and doc.nodes["A"].shape == "rect"
    assert doc.nodes["B"].shape == "round"
    assert list(doc.nodes) == ["A", "B"]


def test_node_shape_form_keyword_line_is_passthrough() -> None:
    doc = parse("flowchart TD\nA@{ shape: cyl, label: 'x' }\nA --> B\ndirection LR\n")
    assert [p.text for p in doc.passthrough] == ["A@{ shape: cyl, label: 'x' }", "direction LR"]
    assert doc.nodes["A"].shape == "bare"


def test_split_statements() -> None:
    assert split_statements("A; B;; C") == ["A", "B", "C"]
    assert split_statements('A["x; y"] --> B') == ['A["x; y"] --> B']
    assert split_statements("A[x; y] --> B; C") == ["A[x; y] --> B", "C"]
    assert split_statements("A -->|a; b| C; D") == ["A -->|a; b| C", "D"]


# ------------------------------------------------------------------ errors --


@pytest.mark.parametrize(
    "text,line,fragment",
    [
        ("A --> B\n", 1, "header"),
        ("flowchart TD\nA --> B\nB -> C\n", 3, "link operator"),
        ("flowchart TD\n    A --> B\n    --> C\n", 3, "node"),
        ("flowchart TD\nA --> B\nend\n", 3, "end"),
        ("flowchart TD\nsubgraph x\nA --> B\n", 2, "subgraph"),
        ("flowchart TD\n%% c\nA --> B\nA -->\n", 4, "node"),
        ("flowchart TD\nA(foo(bar)) --> B\n", 2, "link operator"),
        ("", 1, "header"),
        ("%% only a comment\n", 2, "header"),
    ],
)
def test_error_line_numbers(text: str, line: int, fragment: str) -> None:
    with pytest.raises(MermaidError) as info:
        parse(text)
    err = info.value
    assert isinstance(err, ValueError)
    assert err.line == line
    assert fragment in str(err)
    if line <= len(text.split("\n")) and err.text:
        assert err.text == text.split("\n")[line - 1]


def test_error_text_is_the_raw_line() -> None:
    with pytest.raises(MermaidError) as info:
        parse("flowchart TD\n    A ==> B\n    C ~~> D\n")
    assert info.value.line == 3
    assert info.value.text == "    C ~~> D"


# --------------------------------------------------------------- AST API --


def test_add_node_is_upgrade_only_and_appends() -> None:
    doc = parse("flowchart TD\nA[Alpha] --> B\n")
    doc.add_node("A")  # no label, default shape: must not touch the existing node
    assert doc.nodes["A"].label == "Alpha" and doc.nodes["A"].shape == "rect"
    doc.add_node("A", shape="bare")
    assert doc.nodes["A"].shape == "rect"
    doc.add_node("A", "Alpha 2")
    assert doc.nodes["A"].label == "Alpha 2"
    doc.add_node("A", "Alpha 3", "round")  # existing non-bare shape is kept
    assert doc.nodes["A"].shape == "rect" and doc.nodes["A"].label == "Alpha 3"
    doc.add_node("B", "Beta")  # bare -> rect upgrade
    assert doc.nodes["B"].shape == "rect" and doc.nodes["B"].label == "Beta"
    n = doc.add_node("C", "Gamma", "rhombus")
    assert isinstance(n, Node)
    assert list(doc.nodes) == ["A", "B", "C"]
    assert n.order == doc.edges[-1].order + 1
    doc.add_node("D")
    assert doc.nodes["D"].label is None and doc.nodes["D"].shape == "rect"
    assert list(doc.nodes)[-1] == "D"


def test_add_edge_creates_bare_nodes() -> None:
    doc = MermaidDoc()
    e = doc.add_edge("x", "y", "lbl")
    assert (e.src, e.dst, e.label, e.style, e.head, e.length, e.label_form) == ("x", "y", "lbl", "solid", "arrow", 1, "pipe")
    assert doc.nodes["x"].shape == "bare" and doc.nodes["y"].shape == "bare"
    assert doc.nodes["x"].order == e.order == 0
    e2 = doc.add_edge("x", "y")
    assert e2.order == 1 and len(doc.edges) == 2
    assert doc.edge_pairs() == [("x", "y"), ("x", "y")]


def test_remove_node_cascades() -> None:
    doc = load("subgraphs.mmd")
    assert "A1" in doc.subgraphs[0].members
    doc.remove_node("A1")
    assert "A1" not in doc.nodes
    assert all("A1" not in (e.src, e.dst) for e in doc.edges)
    assert "A1" not in doc.subgraphs[0].members
    assert doc.edge_pairs() == [("B1", "B2"), ("A2", "B1"), ("B2", "C1"), ("C1", "D1")]
    doc.remove_node("does-not-exist")  # no-op


def test_remove_edge_and_rename_label() -> None:
    doc = parse("flowchart TD\nA --> B\nA --> B\nB --> C\n")
    assert doc.remove_edge("A", "B") is True
    assert doc.edge_pairs() == [("B", "C")]
    assert doc.remove_edge("A", "B") is False
    doc.rename_label("B", "Bee")
    assert doc.nodes["B"].label == "Bee" and doc.nodes["B"].shape == "rect"
    doc.nodes["C"].shape = "round"
    doc.rename_label("C", "See")
    assert doc.nodes["C"].shape == "round"
    with pytest.raises(KeyError):
        doc.rename_label("nope", "x")


def test_next_order_after_mutations() -> None:
    doc = parse("flowchart TD\nA --> B\n")
    assert doc.next_order() == 1
    doc.remove_node("B")
    assert doc.next_order() == 1  # A keeps serial 0
    doc.add_node("Z")
    assert doc.nodes["Z"].order == 1
