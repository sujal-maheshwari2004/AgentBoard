import json
from pathlib import Path

import pytest

from whiteboard.files.atomic import SelfWriteRegistry
from whiteboard.files.frontmatter import split_frontmatter
from whiteboard.mermaid import extract_mermaid_blocks, parse
from whiteboard.plan.model import AgentCard, PlanSnapshot, Skeleton
from whiteboard.plan.store import OpsResult, PlanStore
from whiteboard.scaffold import scaffold


@pytest.fixture
def root(tmp_path: Path) -> Path:
    scaffold(tmp_path)
    return tmp_path


@pytest.fixture
def store(root: Path) -> PlanStore:
    s = PlanStore(root, self_writes=SelfWriteRegistry())
    s.load()
    return s


@pytest.fixture
def chain(store: PlanStore) -> PlanStore:
    """node-a <- node-b <- node-c (c depends on b and a)."""
    store.upsert_node("node-a", title="A thing")
    store.upsert_node("node-b", title="B thing", depends_on=["node-a"])
    store.upsert_node("node-c", title="C [x]", type="lld", depends_on=["node-a", "node-b"], interfaces=["f() -> int"])
    return store


def _wb(root: Path) -> Path:
    return root / ".whiteboard"


def _hld_inner(root: Path) -> str:
    return _inner(root, "hld")


def _inner(root: Path, name: str) -> str:
    return extract_mermaid_blocks((_wb(root) / "plan" / f"{name}.md").read_text())[0][2]


def _types(msgs: list[dict]) -> list[str]:
    return [m["type"] for m in msgs]


# ----------------------------------------------------------------- loading


def test_load_empty_scaffold(root: Path) -> None:
    store = PlanStore(root, self_writes=SelfWriteRegistry())
    snap = store.load()
    assert isinstance(snap, PlanSnapshot)
    assert snap.project == root.name and snap.nodes == [] and snap.agents == [] and snap.edges == []
    assert {d.name for d in snap.diagrams} == {"hld", "lld", "er"}
    assert list(store.diagrams) == ["hld", "lld", "er"]
    assert snap.layout.keys() == {"hld", "lld", "er"}
    # PLAN.md replaced the template placeholder; the diagrams were left alone.
    plan_md = (_wb(root) / "PLAN.md").read_text()
    assert plan_md.startswith("# PLAN") and "(no nodes yet)" in plan_md
    assert "%% add nodes" in (_wb(root) / "plan" / "hld.md").read_text()
    assert "%% add nodes" in (_wb(root) / "plan" / "lld.md").read_text()
    assert store.cache_path.exists()
    assert store.invalid == {}


def test_load_without_scaffold_creates_dirs_and_hld(tmp_path: Path) -> None:
    store = PlanStore(tmp_path)
    store.load()
    assert (tmp_path / ".whiteboard" / "plan" / "nodes").is_dir()
    assert (tmp_path / ".whiteboard" / "agents").is_dir()
    assert (tmp_path / ".whiteboard" / "plan" / "hld.md").exists()
    assert list(store.diagrams) == ["hld"]


def test_upsert_nodes_writes_exact_frontmatter_order(root: Path, chain: PlanStore) -> None:
    text = (_wb(root) / "plan" / "nodes" / "node-c.md").read_text()
    assert text == (
        "---\n"
        "id: node-c\n"
        "type: lld\n"
        "title: C [x]\n"
        "status: todo\n"
        "owner: null\n"
        "depends_on:\n"
        "- node-a\n"
        "- node-b\n"
        "interfaces:\n"
        "- f() -> int\n"
        "---\n"
        "\n"
    )
    meta, body = split_frontmatter(text)
    assert list(meta) == ["id", "type", "title", "status", "owner", "depends_on", "interfaces"]
    node = chain.get_node("node-c")
    assert node is not None and node.path == ".whiteboard/plan/nodes/node-c.md"


def test_upsert_nodes_regenerates_hld_and_plan_md(root: Path, chain: PlanStore) -> None:
    inner = _hld_inner(root)
    assert inner == (
        "flowchart TD\n"
        "    %% add nodes\n"
        "    node-a[A thing]\n"
        "    node-b[B thing]\n"
        "    node-a --> node-b\n"
    )
    # node-c is type lld: it is homed on the LLD board, not on HLD.
    assert _inner(root, "lld") == (
        "flowchart TD\n"
        "    %% add nodes\n"
        '    node-c["C [x]"]\n'
    )
    plan_md = (_wb(root) / "PLAN.md").read_text()
    assert "| `node-c` | C [x] | lld | todo | — | `node-a`, `node-b` |" in plan_md
    assert "node-b --> node-c" in plan_md  # the full flowchart carries every edge
    assert "### LLD (`plan/lld.md`)" in plan_md and "### HLD (`plan/hld.md`)" in plan_md
    assert chain.rev == 4  # load wrote PLAN.md once, then three upserts


def test_second_load_is_a_noop(root: Path, chain: PlanStore) -> None:
    files = sorted(p for p in _wb(root).rglob("*.md"))
    before = {p: p.read_bytes() for p in files}
    reg = SelfWriteRegistry()
    again = PlanStore(root, self_writes=reg)
    snap = again.load()
    assert again.writes == 0 and len(reg) == 0 and again.rev == 0
    assert {p: p.read_bytes() for p in files} == before
    assert [n.id for n in snap.nodes] == ["node-a", "node-b", "node-c"]
    assert again.get_node("node-c") == chain.get_node("node-c")
    assert {(e.src, e.dst, e.diagram) for e in snap.edges} == {
        ("node-a", "node-b", "hld"), ("node-a", "node-c", "hld"), ("node-b", "node-c", "hld"),
    }


def test_load_unions_diagram_edges_and_frontmatter_deps(root: Path, chain: PlanStore) -> None:
    # While the server was down: the diagram lost the a->b line, node-c was
    # drawn on hld too and a new box node-d appeared (c --> node-d); node-b's
    # frontmatter gained a dep on node-c.
    hld = _wb(root) / "plan" / "hld.md"
    text = hld.read_text().replace("    node-a --> node-b\n", "")
    hld.write_text(text.replace(
        "    node-b[B thing]\n",
        '    node-b[B thing]\n    node-c["C [x]"]\n    node-b --> node-c\n    node-c --> node-d[D thing]\n',
    ))
    node_b = _wb(root) / "plan" / "nodes" / "node-b.md"
    node_b.write_text(node_b.read_text().replace("depends_on:\n- node-a\n", "depends_on:\n- node-a\n- node-c\n"))

    fresh = PlanStore(root, self_writes=SelfWriteRegistry())
    fresh.load()
    assert fresh.get_node("node-d") is not None
    assert fresh.get_node("node-d").depends_on == ["node-c"]
    assert fresh.get_node("node-d").title == "D thing"
    assert (_wb(root) / "plan" / "nodes" / "node-d.md").exists()
    # union: b keeps both its frontmatter deps; the diagram gets a->b back.
    assert fresh.get_node("node-b").depends_on == ["node-a", "node-c"]
    inner = _hld_inner(root)
    assert "node-a --> node-b" in inner and "node-c --> node-b" in inner and "node-c --> node-d" in inner
    assert fresh.rev == 1


def test_load_records_invalid_node_files(root: Path, chain: PlanStore) -> None:
    bad = _wb(root) / "plan" / "nodes" / "node-bad.md"
    bad.write_text("---\nid: node-bad\ntitle: Bad\nstatus: not-a-status\n---\n")
    mismatch = _wb(root) / "plan" / "nodes" / "node-other.md"
    mismatch.write_text("---\nid: node-elsewhere\ntitle: X\n---\n")
    fresh = PlanStore(root)
    fresh.load()
    assert set(fresh.nodes) == {"node-a", "node-b", "node-c"}
    assert ".whiteboard/plan/nodes/node-bad.md" in fresh.invalid
    assert "does not match file name" in fresh.invalid[".whiteboard/plan/nodes/node-other.md"]
    assert bad.exists()  # never touched


def test_load_keeps_previous_diagram_on_parse_error(root: Path, chain: PlanStore) -> None:
    hld = _wb(root) / "plan" / "hld.md"
    good_inner = _hld_inner(root)
    hld.write_text("# HLD\n\n```mermaid\nflowchart TD\n    node-a[A thing\n```\n")
    fresh = PlanStore(root)
    fresh.load()
    assert ".whiteboard/plan/hld.md" in fresh.invalid
    assert fresh.diagrams["hld"].doc is None and fresh.diagrams["hld"].error
    assert "node-a[A thing\n" in hld.read_text()  # the broken file is never overwritten
    # The in-process store keeps its good doc when the file breaks later.
    chain.diagrams["hld"].text = hld.read_text()
    state = chain._read_diagram_file(hld, chain.diagrams["hld"])
    assert state is not None and state.doc is not None and state.error
    assert parse(good_inner).edge_pairs() == state.doc.edge_pairs()


# ------------------------------------------------------------- snapshots


def test_snapshot_and_skeleton(chain: PlanStore) -> None:
    snap = chain.snapshot()
    assert snap.rev == chain.rev
    assert [n.id for n in snap.nodes] == ["node-a", "node-b", "node-c"]
    hld = next(d for d in snap.diagrams if d.name == "hld")
    assert hld.direction == "TD" and hld.path == ".whiteboard/plan/hld.md"
    assert [(e.src, e.dst) for e in hld.edges] == [("node-a", "node-b")]
    assert hld.mermaid.startswith("flowchart TD\n")
    lld = next(d for d in snap.diagrams if d.name == "lld")
    assert lld.path == ".whiteboard/plan/lld.md" and lld.edges == []
    assert "node-c" in lld.mermaid
    skel = chain.skeleton()
    assert isinstance(skel, Skeleton)
    assert skel.nodes[2] == {"id": "node-c", "title": "C [x]", "type": "lld", "status": "todo", "depends_on": ["node-a", "node-b"]}
    assert json.dumps(snap.model_dump(mode="json"))  # JSON-serializable


def test_collaboration_and_plan_md_text(root: Path, store: PlanStore) -> None:
    assert store.collaboration_text().startswith("# COLLABORATION")
    (_wb(root) / "COLLABORATION.md").unlink()
    assert store.collaboration_text().startswith("# COLLABORATION")  # template fallback
    assert store.plan_md_text().startswith("# PLAN")


# --------------------------------------------------------------- node API


def test_upsert_node_merges_existing_fields_and_body(root: Path, chain: PlanStore) -> None:
    chain.upsert_node("node-b", body="Design notes\n\n```mermaid\nflowchart LR\n    x --> y\n```\n")
    node = chain.get_node("node-b")
    assert node.title == "B thing" and node.depends_on == ["node-a"]
    text = (_wb(root) / "plan" / "nodes" / "node-b.md").read_text()
    assert text.endswith("---\n\nDesign notes\n\n```mermaid\nflowchart LR\n    x --> y\n```\n")
    chain.upsert_node("node-b", status="in_progress", owner="agent-b")
    node = chain.get_node("node-b")
    assert node.status == "in_progress" and node.owner == "agent-b" and node.body.startswith("Design notes")
    assert chain.last_messages[0]["type"] == "plan.node.upsert"
    assert chain.last_messages[0]["payload"]["node"]["owner"] == "agent-b"


def test_upsert_node_validation(chain: PlanStore) -> None:
    with pytest.raises(ValueError):
        chain.upsert_node("Node_Bad", title="x")
    with pytest.raises(ValueError):
        chain.upsert_node("node-x", title="x", type="uml")
    with pytest.raises(ValueError):
        chain.upsert_node("node-x", title="x", status="later")
    with pytest.raises(ValueError):
        chain.upsert_node("node-x", title="x", depends_on=["node-x"])
    assert chain.get_node("node-x") is None


def test_upsert_node_rejects_cycles(chain: PlanStore) -> None:
    with pytest.raises(ValueError, match="cycle"):
        chain.upsert_node("node-a", depends_on=["node-c"])
    assert chain.get_node("node-a").depends_on == []


def test_upsert_node_into_er_diagram(root: Path, chain: PlanStore) -> None:
    chain.upsert_node("node-users", title="Users", type="er")
    chain.upsert_node("node-orders", title="Orders", type="er", depends_on=["node-users"], diagram="er")
    er = extract_mermaid_blocks((_wb(root) / "plan" / "er.md").read_text())[0][2]
    assert "node-users[Users]" in er and "node-users --> node-orders" in er
    assert "node-users" not in _hld_inner(root)
    snap = chain.snapshot()
    assert next(e for e in snap.edges if e.dst == "node-orders").diagram == "er"


def test_upsert_node_into_lld_diagram(root: Path, chain: PlanStore) -> None:
    chain.upsert_node("node-cache", title="Cache", type="lld")
    chain.upsert_node("node-queue", title="Queue", type="lld", depends_on=["node-cache"], diagram="lld")
    lld = _inner(root, "lld")
    assert "node-cache[Cache]" in lld and "node-cache --> node-queue" in lld
    assert "node-cache" not in _hld_inner(root)
    snap = chain.snapshot()
    assert next(e for e in snap.edges if e.dst == "node-queue").diagram == "lld"


def test_set_node_status_and_owner(chain: PlanStore) -> None:
    node = chain.set_node_status("node-a", "done", owner="agent-a")
    assert node.status == "done" and node.owner == "agent-a"
    assert chain.set_node_status("node-a", "todo").owner == "agent-a"
    assert chain.set_node_status("node-a", "todo", owner="").owner is None
    with pytest.raises(ValueError):
        chain.set_node_status("node-zzz", "done")
    with pytest.raises(ValueError):
        chain.set_node_status("node-a", "finished")


def test_delete_node_cascades(root: Path, chain: PlanStore) -> None:
    chain.save_layout("hld", {"nodes": {"node-a": {"x": 1, "y": 2}}, "edges": {"node-a__node-b": {"precise": True}}})
    chain.delete_node("node-a")
    assert not (_wb(root) / "plan" / "nodes" / "node-a.md").exists()
    assert chain.get_node("node-b").depends_on == [] and chain.get_node("node-c").depends_on == ["node-b"]
    inner = _hld_inner(root)
    assert "node-a" not in inner and "node-b[B thing]" in inner
    assert "node-a" not in (_wb(root) / "PLAN.md").read_text()
    layout = json.loads((_wb(root) / "plan" / "hld.layout.json").read_text())
    assert layout["nodes"] == {} and layout["edges"] == {}
    assert _types(chain.last_messages) == [
        "plan.node.upsert", "plan.node.upsert", "plan.edge.delete", "plan.edge.delete", "plan.node.delete",
    ]
    assert chain.last_messages[-1] == {"type": "plan.node.delete", "payload": {"id": "node-a"}}
    with pytest.raises(ValueError):
        chain.delete_node("node-a")


# --------------------------------------------------------------- apply_ops


def test_cosmetic_rename_changes_exactly_one_line(root: Path, chain: PlanStore) -> None:
    path = _wb(root) / "plan" / "nodes" / "node-b.md"
    before = path.read_text().splitlines()
    result = chain.apply_ops([{"op": "renamed", "id": "node-b", "label": "B renamed"}])
    assert result.ok and not result.risky and result.request_id is None
    after = path.read_text().splitlines()
    assert len(before) == len(after)
    changed = [(a, b) for a, b in zip(before, after) if a != b]
    assert changed == [("title: B thing", "title: B renamed")]
    assert "node-b[B renamed]" in _hld_inner(root)
    assert _types(result.messages) == ["plan.node.upsert"]
    assert result.summary == "node-b: renamed to 'B renamed'"
    assert chain.pending_edits == {}


def test_node_created_and_status_changed_are_cosmetic(root: Path, chain: PlanStore) -> None:
    result = chain.apply_ops([
        {"kind": "node-created", "id": "node-d", "label": "D thing", "diagram": "hld"},
        {"type": "status-changed", "id": "node-a", "status": "in_progress"},
        {"op": "node-created", "label": "Ledger table", "diagram": "er"},
        {"op": "node-created", "label": "Cache layer", "diagram": "lld"},
    ])
    assert result.ok, result.error
    assert chain.get_node("node-d").title == "D thing" and chain.get_node("node-d").type == "hld"
    assert chain.get_node("node-a").status == "in_progress"
    assert chain.get_node("node-ledger-table").type == "er"
    assert chain.get_node("node-cache-layer").type == "lld"
    assert (_wb(root) / "plan" / "nodes" / "node-d.md").exists()
    assert "node-d[D thing]" in _hld_inner(root)
    assert "node-ledger-table[Ledger table]" in _inner(root, "er")
    assert "node-cache-layer[Cache layer]" in _inner(root, "lld")
    assert _types(result.messages) == ["plan.node.upsert"] * 4


def test_apply_ops_errors_write_nothing(root: Path, chain: PlanStore) -> None:
    rev = chain.rev
    writes = chain.writes
    cases = [
        [{"op": "renamed", "id": "node-zzz", "label": "x"}],
        [{"op": "node-created", "id": "node-a", "label": "dup"}],
        [{"op": "node-created", "id": "Bad Id", "label": "x"}],
        [{"op": "status-changed", "id": "node-a", "status": "nope"}],
        [{"op": "edge-created", "from": "node-c", "to": "node-a"}],  # cycle
        [{"op": "edge-created", "from": "node-a", "to": "node-a"}],
        [{"op": "explode", "id": "node-a"}],
        [{"op": "renamed", "id": "node-a"}],
        ["not an op"],
    ]
    for ops in cases:
        result = chain.apply_ops(ops)
        assert result.ok is False and result.error, ops
        assert result.request_id is None and result.revert == ops
    assert chain.rev == rev and chain.writes == writes and chain.pending_edits == {}
    assert "cycle" in chain.apply_ops(cases[4]).error


def test_risky_edge_op_parks_then_commits(root: Path, chain: PlanStore) -> None:
    chain.upsert_node("node-d", title="D thing")
    rev = chain.rev
    result = chain.apply_ops([{"op": "edge-created", "from": "node-b", "to": "node-d", "diagram": "hld"}])
    assert isinstance(result, OpsResult)
    assert result.ok is False and result.risky is True and result.request_id
    assert result.summary == "node-d: depends_on +node-b"
    assert result.affected == ["node-d"]
    assert "--- a/.whiteboard/plan/nodes/node-d.md" in result.diff and "+- node-b" in result.diff
    assert chain.get_node("node-d").depends_on == [] and chain.rev == rev
    assert "node-b --> node-d" not in _hld_inner(root)
    assert set(chain.pending_edits) == {result.request_id}

    committed = chain.resolve_pending(result.request_id, True, "because")
    assert committed.ok is True and committed.request_id == result.request_id
    assert _types(committed.messages) == ["plan.node.upsert", "plan.edge.upsert"]
    assert committed.messages[1] == {
        "type": "plan.edge.upsert",
        "payload": {"edge": {"src": "node-b", "dst": "node-d", "label": None, "diagram": "hld"}},
    }
    assert chain.get_node("node-d").depends_on == ["node-b"]
    assert "node-b --> node-d" in _hld_inner(root)
    assert "- node-b" in (_wb(root) / "plan" / "nodes" / "node-d.md").read_text()
    assert chain.rev == rev + 1 and chain.pending_edits == {}


def test_cross_board_edge_attribution(root: Path, chain: PlanStore) -> None:
    """node-c (lld) -> node-d (hld) is drawn on neither board; it is attributed
    to the home board of its source and lives in the frontmatter."""
    chain.upsert_node("node-d", title="D thing")
    result = chain.apply_ops(
        [{"op": "edge-created", "from": "node-c", "to": "node-d"}], request_id="pre-approved"
    )
    assert result.ok
    edge = next(m for m in result.messages if m["type"] == "plan.edge.upsert")
    assert edge["payload"]["edge"] == {"src": "node-c", "dst": "node-d", "label": None, "diagram": "lld"}
    assert "node-c --> node-d" not in _hld_inner(root)
    assert "node-c --> node-d" not in _inner(root, "lld")
    assert "- node-c" in (_wb(root) / "plan" / "nodes" / "node-d.md").read_text()
    snap = chain.snapshot()
    assert next(e for e in snap.edges if e.dst == "node-d").diagram == "lld"


def test_risky_op_rejected_returns_revert(chain: PlanStore) -> None:
    ops = [{"op": "edge-deleted", "from": "node-a", "to": "node-b", "diagram": "hld"}]
    parked = chain.apply_ops(ops)
    assert parked.risky and parked.summary == "node-b: depends_on -node-a"
    result = chain.resolve_pending(parked.request_id, False, "no thanks")
    assert result.ok is False and result.revert == ops and result.request_id == parked.request_id
    assert chain.get_node("node-b").depends_on == ["node-a"]
    assert chain.pending_edits == {}
    assert chain.resolve_pending("nope", True, "").error


def test_risky_delete_and_reroute_ops(root: Path, chain: PlanStore) -> None:
    parked = chain.apply_ops([
        {"op": "edge-rerouted", "from": "node-a", "to": "node-c", "new_from": "node-b", "new_to": "node-c"},
        {"op": "deleted", "id": "node-a"},
    ])
    assert parked.risky and parked.affected == ["node-c", "node-a", "node-b"]
    assert "node-a: deleted node-a" in parked.summary and "node-b: depends_on -node-a" in parked.summary
    assert "+++ /dev/null" in parked.diff
    result = chain.resolve_pending(parked.request_id, True, "ok")
    assert result.ok
    assert set(chain.nodes) == {"node-b", "node-c"}
    assert chain.get_node("node-c").depends_on == ["node-b"]
    assert not (_wb(root) / "plan" / "nodes" / "node-a.md").exists()
    assert "plan.node.delete" in _types(result.messages)


def test_apply_ops_with_request_id_commits_risky_directly(chain: PlanStore) -> None:
    result = chain.apply_ops([{"op": "edge-deleted", "from": "node-b", "to": "node-c"}], request_id="pre-approved")
    assert result.ok and result.risky and result.request_id == "pre-approved"
    assert chain.get_node("node-c").depends_on == ["node-a"]


def test_resolve_pending_fails_if_state_moved_on(chain: PlanStore) -> None:
    parked = chain.apply_ops([{"op": "deleted", "id": "node-c"}])
    chain.delete_node("node-c")
    result = chain.resolve_pending(parked.request_id, True, "")
    assert result.ok is False and "unknown node" in result.error
    assert result.revert == [{"op": "deleted", "id": "node-c"}] and result.request_id == parked.request_id


# ------------------------------------------------------------ diagrams


def test_write_diagram_round_trip(root: Path, chain: PlanStore) -> None:
    mermaid = (
        "flowchart LR\n"
        "    %% custom comment\n"
        "    node-a[A thing]\n"
        "    node-b[B thing]\n"
        "    node-d[Brand new]\n"
        "    node-a --> node-d\n"
        "    node-d -->|feeds| node-b\n"
    )
    diagram = chain.write_diagram("hld", mermaid)
    assert diagram.name == "hld" and diagram.direction == "LR"
    # The returned Diagram is the regenerated one: node-c (not drawn) is re-added with its edges.
    assert [(e.src, e.dst, e.label) for e in diagram.edges] == [
        ("node-a", "node-d", None), ("node-d", "node-b", "feeds"),
    ]
    assert chain.get_node("node-d").title == "Brand new" and chain.get_node("node-d").depends_on == ["node-a"]
    assert chain.get_node("node-b").depends_on == ["node-d"]  # a->b removed by the diagram edit
    # node-c is homed on lld, so rewriting hld does not pull it back in.
    inner = _hld_inner(root)
    assert inner == (
        "flowchart LR\n"
        "    %% custom comment\n"
        "    node-a[A thing]\n"
        "    node-b[B thing]\n"
        "    node-d[Brand new]\n"
        "    node-a --> node-d\n"
        "    node-d -->|feeds| node-b\n"
    )
    assert 'node-c["C [x]"]' in _inner(root, "lld")
    assert (_wb(root) / "plan" / "hld.md").read_text().startswith("# HLD\n")
    again = chain.write_diagram("hld", inner)
    assert again.mermaid == inner
    fresh = PlanStore(root)
    fresh.load()
    assert fresh.writes == 0 and fresh.get_node("node-b").depends_on == ["node-d"]


def test_write_diagram_validation(root: Path, chain: PlanStore) -> None:
    with pytest.raises(ValueError, match="invalid mermaid"):
        chain.write_diagram("hld", "flowchart TD\n    node-a[oops\n")
    with pytest.raises(ValueError, match="Users"):
        chain.write_diagram("er", "flowchart TD\n    Users --> node-a\n")
    with pytest.raises(ValueError, match="cycle"):
        chain.write_diagram("hld", "flowchart TD\n    node-c --> node-a\n    node-a --> node-b\n    node-b --> node-c\n")
    with pytest.raises(ValueError, match="diagram name"):
        chain.write_diagram("HLD!", "flowchart TD\n")
    assert chain.get_node("node-a").depends_on == []
    assert "node-c --> node-a" not in _hld_inner(root)


def test_write_new_diagram_file(root: Path, chain: PlanStore) -> None:
    diagram = chain.write_diagram("api", "flowchart TD\n    node-a --> node-api[API layer]\n")
    path = _wb(root) / "plan" / "api.md"
    # The bare endpoint node-a gets its title as label (frontmatter is authoritative for labels).
    assert path.read_text() == (
        "# API\n\n```mermaid\nflowchart TD\n    node-a[A thing]\n    node-api[API layer]\n    node-a --> node-api\n```\n"
    )
    assert diagram.path == ".whiteboard/plan/api.md"
    assert chain.get_node("node-api").type == "hld" and chain.get_node("node-api").depends_on == ["node-a"]
    assert "node-api[API layer]" in _hld_inner(root)  # home diagram gets it too
    assert chain.snapshot().layout.keys() == {"hld", "lld", "er", "api"}
    assert chain.get_diagram("api") is not None and chain.get_diagram("nope") is None


# ---------------------------------------------------------------- agents


def test_upsert_agent_creates_folder(root: Path, chain: PlanStore) -> None:
    card = chain.upsert_agent("agent-b", "node-b")
    assert isinstance(card, AgentCard)
    folder = _wb(root) / "agents" / "agent-b"
    assert (folder / "card.md").read_text() == (
        "---\nid: agent-b\nassigned_node: node-b\nstatus: idle\nclaude_agent_ref: null\n"
        "ready_deps: []\nspawned_at: null\n---\n\nOptional notes.\n"
    )
    assert (folder / "plan.md").read_text().startswith("# Job spec: node-b")
    assert "node-example" not in (folder / "plan.md").read_text()
    assert (folder / "diagrams.md").read_text().startswith("# Diagrams: agent-b")
    assert "| `agent-b` | `node-b` | idle | — |" in (_wb(root) / "PLAN.md").read_text()
    assert chain.last_messages == [{"type": "agent.card.upsert", "payload": {"agent": card.model_dump(mode="json")}}]

    again = chain.upsert_agent("agent-b", None, status="working", plan_md="# custom\n", claude_agent_ref="ref-1", notes="hi")
    assert again.assigned_node == "node-b" and again.status == "working" and again.claude_agent_ref == "ref-1"
    assert (folder / "plan.md").read_text() == "# custom\n"
    assert (folder / "card.md").read_text().endswith("---\n\nhi")
    assert chain.upsert_agent("agent-b", None).plan_md == "# custom\n"  # None keeps existing
    with pytest.raises(ValueError):
        chain.upsert_agent("agent-b", "node-nope")
    with pytest.raises(ValueError):
        chain.upsert_agent("bad", "node-a")
    with pytest.raises(ValueError):
        chain.upsert_agent("agent-c", "node-a", status="tired")


def test_upsert_agent_prefills_ready_deps_from_done_nodes(chain: PlanStore) -> None:
    chain.set_node_status("node-a", "done")
    card = chain.upsert_agent("agent-c", "node-c")
    assert card.ready_deps == ["node-a"]


def test_set_agent_status_and_ref(root: Path, chain: PlanStore) -> None:
    chain.upsert_agent("agent-b", "node-b")
    card = chain.set_agent_status("agent-b", "working")
    assert card.status == "working"
    assert "status: working" in (_wb(root) / "agents" / "agent-b" / "card.md").read_text()
    assert "| `agent-b` | `node-b` | working | — |" in (_wb(root) / "PLAN.md").read_text()
    ref = chain.set_agent_ref("agent-b", "task-42", spawned_at="2026-09-20T00:00:00.000Z")
    assert ref.claude_agent_ref == "task-42" and ref.spawned_at == "2026-09-20T00:00:00.000Z"
    with pytest.raises(ValueError):
        chain.set_agent_status("agent-zzz", "working")
    with pytest.raises(ValueError):
        chain.set_agent_status("agent-b", "napping")


def test_mark_ready_deps(root: Path, chain: PlanStore) -> None:
    chain.upsert_agent("agent-b", "node-b")
    chain.upsert_agent("agent-c", "node-c")
    chain.upsert_agent("agent-a", "node-a")
    chain.set_node_status("node-a", "done")
    changed = chain.mark_ready_deps("node-a")
    assert [c.id for c in changed] == ["agent-b", "agent-c"]
    assert all(c.ready_deps == ["node-a"] for c in changed)
    assert "ready_deps:\n- node-a\n" in (_wb(root) / "agents" / "agent-c" / "card.md").read_text()
    assert chain.get_agent("agent-a").ready_deps == []
    assert _types(chain.last_messages) == ["agent.card.upsert", "agent.card.upsert"]
    assert chain.mark_ready_deps("node-a") == []  # idempotent
    assert chain.mark_ready_deps("node-zzz") == []
    fresh = PlanStore(root)
    fresh.load()
    assert fresh.get_agent("agent-b").ready_deps == ["node-a"]
    assert fresh.get_agent("agent-b") == chain.get_agent("agent-b")


def test_delete_agent(root: Path, chain: PlanStore) -> None:
    chain.upsert_agent("agent-b", "node-b")
    chain.delete_agent("agent-b")
    assert not (_wb(root) / "agents" / "agent-b").exists()
    assert chain.get_agent("agent-b") is None
    assert chain.last_messages == [{"type": "agent.card.delete", "payload": {"id": "agent-b"}}]


# ---------------------------------------------------------------- layout


def test_save_layout_merges_and_gcs(root: Path, chain: PlanStore) -> None:
    chain.upsert_agent("agent-b", "node-b")
    layout = chain.save_layout("hld", {
        "nodes": {"node-a": {"x": 10, "y": 20, "w": 200, "h": 80}, "node-ghost": {"x": 1}},
        "agents": {"agent-b": {"x": 5}, "agent-ghost": {"x": 6}},
        "edges": {"node-a__node-b": {"precise": True}, "node-a__node-ghost": {}},
        "frames": {"plan-board": {"x": 0, "y": 0}},
    })
    assert layout["nodes"] == {"node-a": {"x": 10, "y": 20, "w": 200, "h": 80}}
    assert layout["agents"] == {"agent-b": {"x": 5}}
    assert layout["edges"] == {"node-a__node-b": {"precise": True}}
    assert layout["frames"] == {"plan-board": {"x": 0, "y": 0}}
    path = _wb(root) / "plan" / "hld.layout.json"
    on_disk = json.loads(path.read_text())
    assert on_disk["nodes"] == layout["nodes"] and on_disk["updatedAt"]
    merged = chain.save_layout("hld", {"nodes": {"node-a": {"x": 11, "pinned": True}}})
    assert merged["nodes"]["node-a"] == {"x": 11, "y": 20, "w": 200, "h": 80, "pinned": True}
    assert chain.snapshot().layout["hld"]["nodes"]["node-a"]["x"] == 11
    fresh = PlanStore(root)
    fresh.load()
    assert fresh.layouts["hld"]["nodes"]["node-a"]["x"] == 11
    with pytest.raises(ValueError):
        chain.save_layout("../etc", {})


# ------------------------------------------------------- last-known-good


def test_last_known_good_cache_written_and_reloaded(root: Path, chain: PlanStore) -> None:
    cache = json.loads((_wb(root) / ".cache" / "last_good.json").read_text())
    assert cache["version"] == 1 and cache["saved_at"].endswith("Z")
    assert cache["nodes"]["node-c"] == {
        "node_id": "node-c", "type": "lld", "status": "todo",
        "depends_on": ["node-a", "node-b"], "interfaces": ["f() -> int"],
    }
    assert ".whiteboard/plan/nodes/node-c.md" in cache["mtimes"] and ".whiteboard/plan/hld.md" in cache["mtimes"]
    fresh = PlanStore(root)
    assert fresh.read_last_good() == cache
    fresh.load()  # rebuilds the cache: same content, new saved_at
    assert fresh.last_good["nodes"] == cache["nodes"] and fresh.last_good["mtimes"] == cache["mtimes"]
    sem = fresh.last_good_semantics()
    assert sem["node-c"].depends_on == frozenset({"node-a", "node-b"}) and sem["node-c"].interfaces == ("f() -> int",)
    chain.set_node_status("node-a", "done")
    assert json.loads((_wb(root) / ".cache" / "last_good.json").read_text())["nodes"]["node-a"]["status"] == "done"
    (_wb(root) / ".cache" / "last_good.json").write_text("garbage")
    assert PlanStore(root).read_last_good() == {}
