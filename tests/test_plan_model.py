import json

import pytest
from pydantic import ValidationError

from whiteboard.plan.model import (
    AGENT_FRONTMATTER_KEYS,
    LIVE_AGENT_FIELDS,
    AgentCard,
    AgentDiagram,
    Diagram,
    Edge,
    Event,
    Node,
    PlanSnapshot,
    Skeleton,
    interface_text,
    node_ready,
    parse_agent_diagram,
    slugify,
    validate_agent_id,
    validate_node_id,
)


# -- id validation ------------------------------------------------------------


@pytest.mark.parametrize("good", ["node-a", "node-parser-v2", "node-0", "node-a1-b2"])
def test_validate_node_id_accepts(good):
    assert validate_node_id(good) == good


@pytest.mark.parametrize("bad", ["node-", "node--a", "Node-a", "node-A", "agent-a", "node-a b", "", "node-_x"])
def test_validate_node_id_rejects(bad):
    with pytest.raises(ValueError):
        validate_node_id(bad)


def test_validate_agent_id():
    assert validate_agent_id("agent-parser") == "agent-parser"
    for bad in ["node-parser", "agent-", "agent-Parser", None]:
        with pytest.raises(ValueError):
            validate_agent_id(bad)  # type: ignore[arg-type]


def test_slugify():
    assert slugify("Mermaid parser v2") == "mermaid-parser-v2"
    assert slugify("  Hello,  World!  ") == "hello-world"
    assert slugify("Ünïcödé Ñame") == "unicode-name"
    assert slugify("---") == "untitled"
    assert slugify("") == "untitled"
    for t in ["Mermaid parser v2", "!!!", "A/B testing", "日本語"]:
        validate_node_id(f"node-{slugify(t)}")


# -- Node ---------------------------------------------------------------------


def test_node_defaults_and_validation():
    n = Node(id="node-a", title="A")
    assert n.type == "hld" and n.status == "todo" and n.owner is None
    assert n.depends_on == [] and n.interfaces == [] and n.body == "" and n.extra == {}
    with pytest.raises(ValidationError):
        Node(id="bad", title="x")
    with pytest.raises(ValidationError):
        Node(id="node-a", title="x", status="wip")
    with pytest.raises(ValidationError):
        Node(id="node-a", title="x", type="xxx")
    with pytest.raises(ValidationError):
        Node(id="node-a", title="x", owner="not-an-agent")


def test_node_depends_on_validated_and_deduped():
    n = Node(id="node-a", title="A", depends_on=["node-b", "node-c", "node-b", " node-c "])
    assert n.depends_on == ["node-b", "node-c"]
    with pytest.raises(ValidationError):
        Node(id="node-a", title="A", depends_on=["node-b", "bogus"])


def test_node_interfaces_accept_strings_and_dicts():
    n = Node(id="node-a", title="A", interfaces=["parse(text) -> Doc", {"name": "serialize", "kind": "fn"}, ""])
    assert n.interfaces == ["parse(text) -> Doc", {"name": "serialize", "kind": "fn"}]
    assert interface_text(n.interfaces[0]) == "parse(text) -> Doc"
    assert interface_text(n.interfaces[1]) == "serialize (fn)"
    assert interface_text({"name": "only"}) == "only"


def test_node_frontmatter_order_and_extra():
    n = Node(id="node-a", title="A", status="done", owner="agent-x", depends_on=["node-b"],
             interfaces=["f()"], extra={"priority": 2, "tags": ["x"]})
    fm = n.frontmatter()
    assert list(fm) == ["id", "type", "title", "status", "owner", "depends_on", "interfaces", "priority", "tags"]
    assert fm["owner"] == "agent-x" and fm["depends_on"] == ["node-b"]
    # extra never shadows a canonical key
    n2 = Node(id="node-a", title="A", extra={"id": "hacked", "z": 1})
    assert n2.frontmatter()["id"] == "node-a" and n2.frontmatter()["z"] == 1


def test_node_from_frontmatter_tolerant_round_trip():
    meta = {"id": "node-parser", "type": "lld", "title": "Mermaid parser", "status": "todo",
            "owner": None, "depends_on": ["node-files"], "interfaces": ["parse(text) -> MermaidDoc"],
            "custom": {"a": 1}}
    n = Node.from_frontmatter(meta, "Body text\n", ".whiteboard/plan/nodes/node-parser.md")
    assert n.title == "Mermaid parser" and n.type == "lld" and n.extra == {"custom": {"a": 1}}
    assert n.body == "Body text\n" and n.path == ".whiteboard/plan/nodes/node-parser.md"
    assert n.frontmatter() == {**{k: meta[k] for k in ("id", "type", "title", "status", "owner", "depends_on", "interfaces")}, "custom": {"a": 1}}


def test_node_from_frontmatter_missing_fields():
    n = Node.from_frontmatter({"id": "node-x"}, "", None)
    assert n.title == "node-x" and n.type == "hld" and n.status == "todo"
    n = Node.from_frontmatter({"title": ""}, "", "/p/.whiteboard/plan/nodes/node-from-path.md")
    assert n.id == "node-from-path" and n.title == "node-from-path"
    n = Node.from_frontmatter({"id": "node-y", "type": None, "status": None, "depends_on": None,
                               "interfaces": None, "owner": "null"}, None, None)  # type: ignore[arg-type]
    assert n.type == "hld" and n.status == "todo" and n.depends_on == [] and n.owner is None
    with pytest.raises(ValidationError):
        Node.from_frontmatter({}, "", None)


def test_node_ready():
    nodes = {
        "node-a": Node(id="node-a", title="A", status="done"),
        "node-b": Node(id="node-b", title="B", status="todo"),
        "node-c": Node(id="node-c", title="C", depends_on=["node-a"]),
        "node-d": Node(id="node-d", title="D", depends_on=["node-a", "node-b"]),
        "node-e": Node(id="node-e", title="E", depends_on=["node-zzz"]),
    }
    assert node_ready(nodes["node-a"], nodes)
    assert node_ready(nodes["node-c"], nodes)
    assert not node_ready(nodes["node-d"], nodes)
    assert not node_ready(nodes["node-e"], nodes)


# -- AgentCard ----------------------------------------------------------------


def test_agent_card_validation_and_frontmatter():
    a = AgentCard(id="agent-parser", assigned_node="node-parser", ready_deps=["node-a", "node-a"])
    assert a.status == "idle" and a.ready_deps == ["node-a"]
    assert list(a.frontmatter()) == ["id", "assigned_node", "status", "claude_agent_ref", "ready_deps", "spawned_at"]
    with pytest.raises(ValidationError):
        AgentCard(id="node-parser", assigned_node=None)
    with pytest.raises(ValidationError):
        AgentCard(id="agent-x", assigned_node="bad")
    with pytest.raises(ValidationError):
        AgentCard(id="agent-x", assigned_node=None, status="running")


def test_agent_card_from_frontmatter_round_trip():
    meta = {"id": "agent-parser", "assigned_node": "node-parser", "status": "working",
            "claude_agent_ref": "abc", "ready_deps": ["node-files"], "spawned_at": "2026-09-20T00:00:00Z",
            "note_to_self": "x"}
    a = AgentCard.from_frontmatter(meta, "Notes.\n", plan_md="# spec", diagrams_md="```mermaid\n```")
    assert a.notes == "Notes.\n" and a.plan_md == "# spec" and a.diagrams_md.startswith("```")
    assert a.extra == {"note_to_self": "x"}
    fm = a.frontmatter()
    assert fm == meta
    a2 = AgentCard.from_frontmatter({"id": "agent-z", "status": None, "assigned_node": "null"}, "")
    assert a2.status == "idle" and a2.assigned_node is None


MERMAID_MD = (
    "# Diagrams: agent-parser\n\n```mermaid\nflowchart LR\n"
    "    Lexer[Lexer] -->|tokens| Parser[Parser]\n    Parser --> Ast\n```\n"
)


def test_parse_agent_diagram():
    assert parse_agent_diagram(None) is None
    assert parse_agent_diagram("") is None
    assert parse_agent_diagram("# Diagrams\n\nNo fence here yet.\n") is None

    d = parse_agent_diagram(MERMAID_MD)
    assert isinstance(d, AgentDiagram) and d.error is None and d.direction == "LR"
    # box ids are free-form here: these are components, not plan node ids
    assert d.nodes == [
        {"id": "Lexer", "label": "Lexer"}, {"id": "Parser", "label": "Parser"}, {"id": "Ast", "label": "Ast"},
    ]
    assert d.edges == [
        {"src": "Lexer", "dst": "Parser", "label": "tokens"}, {"src": "Parser", "dst": "Ast", "label": None},
    ]
    assert d.mermaid.startswith("flowchart LR\n")

    # a broken fence is captured, never raised
    broken = parse_agent_diagram("```mermaid\nflowchart TD\n    A[[[\n```\n")
    assert broken is not None and broken.error and "line 2" in broken.error
    assert broken.nodes == [] and broken.mermaid == "flowchart TD\n    A[[[\n"


def test_agent_card_diagram_derived_and_round_trips():
    card = AgentCard(id="agent-parser", assigned_node="node-parser", diagrams_md=MERMAID_MD)
    assert card.diagram is not None and [n["id"] for n in card.diagram.nodes] == ["Lexer", "Parser", "Ast"]
    # derived, not stored: it is absent from the frontmatter and a supplied
    # value is ignored in favour of what diagrams_md actually says
    assert "diagram" not in card.frontmatter()
    forced = AgentCard(id="agent-parser", diagrams_md=MERMAID_MD, diagram={"mermaid": "lies", "nodes": []})
    assert forced.diagram == card.diagram
    assert AgentCard(id="agent-parser").diagram is None
    dumped = card.model_dump(mode="json")
    assert dumped["diagram"]["edges"][0] == {"src": "Lexer", "dst": "Parser", "label": "tokens"}
    assert AgentCard.model_validate(dumped) == card
    assert json.dumps(dumped)


def test_agent_card_live_fields_written_only_when_set():
    assert len(AGENT_FRONTMATTER_KEYS) == 11
    assert AGENT_FRONTMATTER_KEYS[6:] == LIVE_AGENT_FIELDS
    bare = AgentCard(id="agent-a", assigned_node="node-a")
    assert list(bare.frontmatter()) == list(AGENT_FRONTMATTER_KEYS[:6])
    live = AgentCard(
        id="agent-a", assigned_node="node-a", activity="running tests", progress=1.7,
        heartbeat_at="2026-09-20T00:00:01Z", finished_at=None,
        metrics={"tool_calls": 4, "files_touched": ["a.py", "a.py", "b.py"]},
    )
    assert live.progress == 1.0  # clamped
    assert live.metrics["files_touched"] == ["a.py", "b.py"]
    fm = live.frontmatter()
    assert list(fm) == [*AGENT_FRONTMATTER_KEYS[:6], "activity", "progress", "heartbeat_at", "metrics"]
    assert "finished_at" not in fm
    assert AgentCard.from_frontmatter(fm, "notes\n") == live.model_copy(update={"notes": "notes\n"})
    assert AgentCard(id="agent-a", progress=-3).progress == 0.0
    capped = AgentCard(id="agent-a", metrics={"files_touched": [f"f{i}.py" for i in range(500)]})
    assert len(capped.metrics["files_touched"]) == 200


# -- Event / Skeleton / Diagram / PlanSnapshot ----------------------------------


def test_event_model_dump_json_safe():
    e = Event(seq=1, ts="2026-09-20T00:14:22.123Z", agent_id="agent-a", node_id=None, type="done",
              note=None, notified=None, data={"ops": [{"op": "renamed"}]})
    d = e.model_dump()
    assert d == {"seq": 1, "ts": "2026-09-20T00:14:22.123Z", "agent_id": "agent-a", "node_id": None,
                 "type": "done", "note": "", "notified": [], "data": {"ops": [{"op": "renamed"}]}}
    json.dumps(d)
    # extra keys from older/newer writers are ignored, not fatal
    e2 = Event.model_validate({**d, "unknown": 1})
    assert e2.seq == 1


def test_skeleton_diagram_snapshot():
    sk = Skeleton(project="/p", nodes=[{"id": "node-a", "title": "A", "type": "hld", "status": "todo", "depends_on": []}])
    assert sk.nodes[0]["id"] == "node-a"
    d = Diagram(name="hld", direction="TD", edges=[Edge(src="node-a", dst="node-b")], mermaid="flowchart TD\n", path="plan/hld.md")
    snap = PlanSnapshot(project="/p", rev=3, nodes=[Node(id="node-a", title="A")], edges=d.edges,
                        agents=[AgentCard(id="agent-a", assigned_node="node-a")], diagrams=[d],
                        layout={"hld": {"nodes": {}}})
    out = snap.model_dump(mode="json")
    assert out["rev"] == 3 and out["diagrams"][0]["edges"][0] == {"src": "node-a", "dst": "node-b", "label": None, "diagram": "hld"}
    assert PlanSnapshot.model_validate(out) == snap
