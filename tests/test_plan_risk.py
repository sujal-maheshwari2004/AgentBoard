from whiteboard.plan.model import Node
from whiteboard.plan.risk import (
    Change,
    NodeSemantics,
    classify_ops,
    describe,
    diff_node,
    graph_problems,
    is_risky,
    risk_score,
)


def sem(**kw) -> NodeSemantics:
    base = dict(id="node-a", title="A")
    base.update(kw)
    return NodeSemantics.from_node(Node(**base))


def test_from_node_normalizes():
    s = NodeSemantics.from_node(Node(id="node-a", title="A", type="lld", status="blocked",
                                     depends_on=["node-b", "node-c"], interfaces=["f()", {"name": "g", "kind": "fn"}]))
    assert s == NodeSemantics("node-a", "lld", "blocked", frozenset({"node-b", "node-c"}), ("f()", "g (fn)"))
    assert hash(s)


def test_created_and_deleted():
    assert diff_node(None, None) == []
    created = diff_node(None, sem())
    assert created == [Change(field="created", after="node-a", weight=0)]
    assert risk_score(created) == 0 and not is_risky(created)
    deleted = diff_node(sem(), None)
    assert deleted[0].field == "deleted" and deleted[0].weight == 100
    assert is_risky(deleted) and is_risky(deleted, threshold=10_000)


def test_weights():
    assert diff_node(sem(), sem()) == []
    assert risk_score(diff_node(sem(), sem(type="lld"))) == 100
    assert risk_score(diff_node(sem(), sem(status="done"))) == 5
    assert risk_score(diff_node(sem(), sem(depends_on=["node-b"]))) == 40
    assert risk_score(diff_node(sem(depends_on=["node-b"]), sem())) == 80  # removal x2
    assert risk_score(diff_node(sem(depends_on=["node-b"]), sem(depends_on=["node-c"]))) == 120
    assert risk_score(diff_node(sem(), sem(interfaces=["f()"]))) == 30
    assert risk_score(diff_node(sem(interfaces=["f()"]), sem())) == 60
    assert risk_score(diff_node(sem(interfaces=["f()"]), sem(interfaces=["f()", "g()"], status="done"))) == 35


def test_is_risky_threshold():
    assert not is_risky(diff_node(sem(), sem(status="done")))
    assert is_risky(diff_node(sem(), sem(interfaces=["f()"])))  # 30 >= 30
    assert not is_risky(diff_node(sem(), sem(interfaces=["f()"])), threshold=31)
    assert is_risky(diff_node(sem(), sem(depends_on=["node-b"])))


def test_change_fields_and_describe():
    changes = diff_node(sem(depends_on=["node-b"], interfaces=["f()"], status="todo"),
                        sem(depends_on=["node-c"], interfaces=["g()"], status="in_progress", type="er"))
    by_field = {c.field: c for c in changes}
    assert set(by_field) == {"type", "depends_on", "interfaces", "status"}
    assert by_field["depends_on"].added == ("node-c",) and by_field["depends_on"].removed == ("node-b",)
    assert by_field["interfaces"].added == ("g()",) and by_field["interfaces"].removed == ("f()",)
    assert by_field["status"].before == "todo" and by_field["status"].after == "in_progress"
    text = describe(changes)
    lines = text.splitlines()
    assert len(lines) == 4
    assert "type 'hld' -> 'er'" in lines[0]
    assert "depends_on +node-c -node-b" in text
    assert "interfaces +g() -f()" in text
    assert describe([]) == "no semantic changes"
    assert describe(diff_node(sem(), None)) == "deleted node-a"
    assert describe(diff_node(None, sem())) == "created node-a"


def test_graph_problems():
    nodes = {
        "node-a": Node(id="node-a", title="A", depends_on=["node-b", "node-zzz"]),
        "node-b": Node(id="node-b", title="B", depends_on=["node-a"]),
        "node-c": Node(id="node-c", title="C"),
    }
    problems = graph_problems(nodes)
    assert len(problems) == 2
    assert problems[0] == "node-a depends on unknown node(s): node-zzz"
    assert problems[1].startswith("dependency cycle: ") and "node-a" in problems[1] and "node-b" in problems[1]
    assert graph_problems({"node-c": nodes["node-c"]}) == []


def test_classify_ops():
    ops = [
        {"op": "renamed", "id": "node-a", "label": "A2"},
        {"op": "node-created", "id": "node-n", "label": "N", "shape": "rect", "diagram": "hld"},
        {"op": "status-changed", "id": "node-a", "status": "done"},
        {"op": "deleted", "id": "node-a"},
        {"op": "edge-created", "from": "node-a", "to": "node-b", "diagram": "hld"},
        {"op": "edge-deleted", "from": "node-a", "to": "node-b", "diagram": "hld"},
        {"op": "edge-rerouted", "from": "node-a", "to": "node-b", "new_from": "node-a", "new_to": "node-c", "diagram": "hld"},
        {"kind": "renamed", "id": "node-b", "label": "B"},
        {"type": "edge-created", "from": "node-b", "to": "node-c"},
        {"op": "something-new"},
    ]
    cosmetic, risky = classify_ops(ops)
    assert [o.get("op") or o.get("kind") for o in cosmetic] == ["renamed", "node-created", "status-changed", "renamed"]
    assert [o.get("op") or o.get("type") for o in risky] == ["deleted", "edge-created", "edge-deleted", "edge-rerouted", "edge-created", "something-new"]
    assert classify_ops([]) == ([], [])
