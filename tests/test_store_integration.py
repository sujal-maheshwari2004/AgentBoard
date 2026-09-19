"""External-edit flows: files change on disk, `apply_file_change` reconciles."""

import asyncio
from pathlib import Path

import pytest

from whiteboard.files.atomic import SelfWriteRegistry
from whiteboard.files.watcher import debounce_worker, start_observer, stop_observer
from whiteboard.mermaid import extract_mermaid_blocks
from whiteboard.plan.store import PlanStore
from whiteboard.scaffold import scaffold


@pytest.fixture
def root(tmp_path: Path) -> Path:
    scaffold(tmp_path)
    return tmp_path


@pytest.fixture
def store(root: Path) -> PlanStore:
    s = PlanStore(root, self_writes=SelfWriteRegistry())
    s.load()
    s.upsert_node("node-a", title="A thing")
    s.upsert_node("node-b", title="B thing", depends_on=["node-a"])
    s.upsert_node("node-c", title="C thing", depends_on=["node-a", "node-b"])
    return s


def _wb(root: Path) -> Path:
    return root / ".whiteboard"


def _hld(root: Path) -> Path:
    return _wb(root) / "plan" / "hld.md"


def _hld_inner(root: Path) -> str:
    return extract_mermaid_blocks(_hld(root).read_text())[0][2]


def _types(msgs: list[dict]) -> list[str]:
    return [m["type"] for m in msgs]


def _change(store: PlanStore, path: Path) -> list[dict]:
    return asyncio.run(store.apply_file_change(str(path)))


# ------------------------------------------------------------- node files


def test_external_node_edit_changes_deps_and_regenerates_diagram(root: Path, store: PlanStore) -> None:
    path = _wb(root) / "plan" / "nodes" / "node-c.md"
    edited = path.read_text().replace("- node-b\n", "")
    path.write_text(edited)
    rev = store.rev
    msgs = _change(store, path)
    assert _types(msgs) == ["plan.node.upsert", "plan.edge.delete", "event.external"]
    assert msgs[0]["payload"]["node"]["depends_on"] == ["node-a"]
    assert msgs[1] == {"type": "plan.edge.delete", "payload": {"src": "node-b", "dst": "node-c", "diagram": "hld"}}
    assert msgs[2]["payload"] == {
        "summary": "node-c: depends_on -node-b",
        "risky": True,
        "path": ".whiteboard/plan/nodes/node-c.md",
        "kind": "node",
        "ids": ["node-c"],
    }
    assert store.get_node("node-c").depends_on == ["node-a"]
    assert "node-b --> node-c" not in _hld_inner(root) and "node-a --> node-c" in _hld_inner(root)
    assert store.rev == rev + 1
    assert store.last_messages == msgs
    # The node file itself was not rewritten (the edit is authoritative).
    assert path.read_text() == edited


def test_external_node_edit_adds_dep_and_title(root: Path, store: PlanStore) -> None:
    path = _wb(root) / "plan" / "nodes" / "node-b.md"
    text = path.read_text().replace("title: B thing", "title: B v2").replace("depends_on:\n- node-a\n", "depends_on:\n- node-a\n- node-d\n")
    path.write_text(text)
    store.upsert_node("node-d", title="D thing")
    msgs = _change(store, path)
    assert _types(msgs) == ["plan.node.upsert", "plan.edge.upsert", "event.external"]
    assert msgs[1]["payload"]["edge"] == {"src": "node-d", "dst": "node-b", "label": None, "diagram": "hld"}
    assert msgs[2]["payload"]["summary"] == "node-b: depends_on +node-d\nnode-b: renamed to 'B v2'"
    assert "node-b[B v2]" in _hld_inner(root) and "node-d --> node-b" in _hld_inner(root)


def test_external_body_only_edit_is_quiet_upsert(root: Path, store: PlanStore) -> None:
    path = _wb(root) / "plan" / "nodes" / "node-a.md"
    path.write_text(path.read_text() + "Some design notes.\n")
    msgs = _change(store, path)
    assert _types(msgs) == ["plan.node.upsert"]
    assert store.get_node("node-a").body == "Some design notes.\n"
    # Identical rewrite (editor touch) -> nothing.
    path.write_text(path.read_text())
    assert _change(store, path) == []


def test_external_node_file_created(root: Path, store: PlanStore) -> None:
    path = _wb(root) / "plan" / "nodes" / "node-new.md"
    path.write_text("---\nid: node-new\ntitle: New thing\ndepends_on: [node-c]\n---\n\nbody\n")
    msgs = _change(store, path)
    assert _types(msgs) == ["plan.node.upsert", "plan.edge.upsert", "event.external"]
    assert msgs[2]["payload"]["summary"] == "node-new: created node-new" and msgs[2]["payload"]["risky"] is False
    assert store.get_node("node-new").type == "hld" and store.get_node("node-new").depends_on == ["node-c"]
    assert "node-c --> node-new" in _hld_inner(root)
    # Tolerant frontmatter (missing keys) is left as the user wrote it.
    assert path.read_text().startswith("---\nid: node-new\ntitle: New thing\n")


def test_external_node_file_deleted(root: Path, store: PlanStore) -> None:
    path = _wb(root) / "plan" / "nodes" / "node-a.md"
    path.unlink()
    msgs = _change(store, path)
    assert _types(msgs) == [
        "plan.node.upsert", "plan.node.upsert", "plan.edge.delete", "plan.edge.delete", "plan.node.delete", "event.external",
    ]
    assert msgs[-2] == {"type": "plan.node.delete", "payload": {"id": "node-a"}}
    assert msgs[-1]["payload"]["summary"] == "deleted node-a" and msgs[-1]["payload"]["risky"] is True
    assert store.get_node("node-a") is None and store.get_node("node-b").depends_on == []
    assert "node-a" not in _hld_inner(root)
    assert _change(store, path) == []  # idempotent


def test_external_invalid_node_file_reported(root: Path, store: PlanStore) -> None:
    path = _wb(root) / "plan" / "nodes" / "node-a.md"
    path.write_text("---\nid: node-a\ntitle: A\nstatus: bogus\n---\n")
    msgs = _change(store, path)
    assert _types(msgs) == ["event.external"] and msgs[0]["payload"]["error"]
    assert ".whiteboard/plan/nodes/node-a.md" in store.invalid
    assert store.get_node("node-a").status == "todo"  # previous good state kept
    path.write_text("---\nid: node-a\ntitle: A\nstatus: done\n---\n")
    msgs = _change(store, path)
    assert msgs[0]["type"] == "plan.node.upsert" and store.get_node("node-a").status == "done"
    assert ".whiteboard/plan/nodes/node-a.md" not in store.invalid


# --------------------------------------------------------------- diagrams


def test_external_diagram_edit_creates_node_and_syncs_deps(root: Path, store: PlanStore) -> None:
    hld = _hld(root)
    text = hld.read_text().replace("```\n\n", "").rstrip("\n")
    assert text.endswith("```")
    hld.write_text(text[:-3] + "    node-new[New thing]\n    node-c --> node-new\n```\n")
    msgs = _change(store, hld)
    assert _types(msgs) == ["plan.node.upsert", "plan.edge.upsert", "event.external"]
    node = store.get_node("node-new")
    assert node is not None and node.title == "New thing" and node.type == "hld" and node.status == "todo"
    assert node.depends_on == ["node-c"]
    file_text = (_wb(root) / "plan" / "nodes" / "node-new.md").read_text()
    assert file_text.startswith("---\nid: node-new\ntype: hld\ntitle: New thing\nstatus: todo\nowner: null\ndepends_on:\n- node-c\n")
    assert msgs[1]["payload"]["edge"] == {"src": "node-c", "dst": "node-new", "label": None, "diagram": "hld"}
    assert msgs[2]["payload"]["summary"] == "node-new: created node-new"
    assert msgs[2]["payload"]["kind"] == "diagram" and msgs[2]["payload"]["ids"] == ["node-new"]
    # Diagram rewritten canonically (heading kept, node declared before edges).
    assert _hld(root).read_text().startswith("# HLD\n")
    assert _hld_inner(root).index("node-new[New thing]") < _hld_inner(root).index("node-c --> node-new")


def test_external_diagram_edit_removes_and_adds_edges(root: Path, store: PlanStore) -> None:
    hld = _hld(root)
    text = hld.read_text().replace("    node-a --> node-b\n", "").replace("    node-b --> node-c\n", "    node-c --> node-b\n")
    hld.write_text(text)
    msgs = _change(store, hld)
    assert _types(msgs) == ["plan.node.upsert", "plan.node.upsert", "plan.edge.delete", "plan.edge.delete", "plan.edge.upsert", "event.external"]
    assert store.get_node("node-b").depends_on == ["node-c"]
    assert store.get_node("node-c").depends_on == ["node-a"]
    summary = msgs[-1]["payload"]["summary"]
    assert "node-b: depends_on +node-c -node-a" in summary and "node-c: depends_on -node-b" in summary
    assert msgs[-1]["payload"]["risky"] is True
    inner = _hld_inner(root)
    assert "node-a --> node-b" not in inner and "node-c --> node-b" in inner and "node-b --> node-c" not in inner


def test_external_diagram_er_creates_er_nodes(root: Path, store: PlanStore) -> None:
    er = _wb(root) / "plan" / "er.md"
    er.write_text("# ER\n\n```mermaid\nflowchart LR\n    node-users[(Users)] --> node-orders[(Orders)]\n```\n")
    msgs = _change(store, er)
    assert _types(msgs) == ["plan.node.upsert", "plan.node.upsert", "plan.edge.upsert", "event.external"]
    assert store.get_node("node-users").type == "er" and store.get_node("node-orders").depends_on == ["node-users"]
    assert msgs[2]["payload"]["edge"]["diagram"] == "er"
    assert "node-users" not in _hld_inner(root)
    er_inner = extract_mermaid_blocks(er.read_text())[0][2]
    assert er_inner == "flowchart LR\n    node-users[(Users)]\n    node-orders[(Orders)]\n    node-users --> node-orders\n"


def test_external_diagram_parse_error_keeps_state(root: Path, store: PlanStore) -> None:
    hld = _hld(root)
    good = hld.read_text()
    hld.write_text("# HLD\n\n```mermaid\nflowchart TD\n    node-a[A thing\n```\n")
    msgs = _change(store, hld)
    assert _types(msgs) == ["event.external"] and "parse error" in msgs[0]["payload"]["summary"]
    assert store.get_node("node-b").depends_on == ["node-a"]
    assert store.diagrams["hld"].doc is not None and store.diagrams["hld"].error
    assert ".whiteboard/plan/hld.md" in store.invalid
    assert "node-a[A thing\n" in hld.read_text()  # not clobbered
    hld.write_text(good)
    assert _change(store, hld) == []
    assert store.diagrams["hld"].error is None and ".whiteboard/plan/hld.md" not in store.invalid


def test_external_diagram_invalid_box_ids_are_dropped(root: Path, store: PlanStore) -> None:
    hld = _hld(root)
    hld.write_text(hld.read_text().replace("    node-a --> node-b\n", "    Legend[Just a note]\n    node-a --> node-b\n"))
    msgs = _change(store, hld)
    assert "Legend" not in _hld_inner(root)
    assert any(k.endswith("#Legend") for k in store.invalid)
    assert store.get_node("node-b").depends_on == ["node-a"]
    assert msgs == []  # nothing semantic changed; only the diagram text was normalized


def test_external_hld_deleted_is_recreated(root: Path, store: PlanStore) -> None:
    hld = _hld(root)
    hld.unlink()
    assert _change(store, hld) == []
    assert hld.exists() and "node-a --> node-b" in _hld_inner(root)


# ---------------------------------------------------------- echo + misc


def test_echo_suppression(root: Path, store: PlanStore) -> None:
    hld = _hld(root)
    node_a = _wb(root) / "plan" / "nodes" / "node-a.md"
    plan_md = _wb(root) / "PLAN.md"
    store.upsert_node("node-a", title="A renamed")
    assert _change(store, node_a) == []
    assert _change(store, hld) == []
    assert _change(store, plan_md) == []
    # One-shot: a second event for the same content is treated as an external
    # (but no-op) edit and still yields nothing.
    assert _change(store, node_a) == []


def test_unrelated_paths_are_ignored(root: Path, store: PlanStore) -> None:
    assert _change(store, _wb(root) / "events.jsonl") == []
    assert _change(store, _wb(root) / "COLLABORATION.md") == []
    assert _change(store, _wb(root) / ".cache" / "last_good.json") == []
    assert _change(store, _wb(root) / "plan" / "hld.layout.json") == []
    assert _change(store, root / "README.md") == []
    assert _change(store, _wb(root) / "plan" / "nodes" / ".node-a.md.tmp.1") == []


def test_external_agent_card_edits(root: Path, store: PlanStore) -> None:
    store.upsert_agent("agent-b", "node-b")
    card = _wb(root) / "agents" / "agent-b" / "card.md"
    assert _change(store, card) == []  # echo
    card.write_text(card.read_text().replace("status: idle", "status: blocked"))
    msgs = _change(store, card)
    assert _types(msgs) == ["agent.card.upsert"] and msgs[0]["payload"]["agent"]["status"] == "blocked"
    assert store.get_agent("agent-b").status == "blocked"
    plan = _wb(root) / "agents" / "agent-b" / "plan.md"
    plan.write_text("# rewritten spec\n")
    msgs = _change(store, plan)
    assert _types(msgs) == ["agent.card.upsert"] and store.get_agent("agent-b").plan_md == "# rewritten spec\n"
    card.unlink()
    assert _change(store, card) == [{"type": "agent.card.delete", "payload": {"id": "agent-b"}}]
    assert store.get_agent("agent-b") is None
    assert "agent-b" not in (_wb(root) / "PLAN.md").read_text()


def test_external_edits_survive_reload(root: Path, store: PlanStore) -> None:
    hld = _hld(root)
    text = hld.read_text().replace("```\n\n", "").rstrip("\n")
    hld.write_text(text[:-3] + "    node-new[New thing]\n    node-c --> node-new\n```\n")
    _change(store, hld)
    fresh = PlanStore(root)
    fresh.load()
    assert fresh.writes == 0
    assert fresh.get_node("node-new") == store.get_node("node-new")
    assert fresh.snapshot().model_dump(mode="json")["edges"] == store.snapshot().model_dump(mode="json")["edges"]


# ------------------------------------------------- real watcher round trip


@pytest.mark.slow
async def test_watcher_to_store_round_trip(root: Path) -> None:
    reg = SelfWriteRegistry()
    store = PlanStore(root, self_writes=reg)
    store.load()
    store.upsert_node("node-a", title="A thing")

    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[str] = asyncio.Queue()
    results: list[tuple[str, list[dict]]] = []
    done = asyncio.Event()

    async def handler(path: str) -> None:
        msgs = await store.apply_file_change(path)
        results.append((path, msgs))
        if any(m["type"] == "event.external" for m in msgs):
            done.set()

    observer = start_observer(loop, queue, root)
    worker = asyncio.create_task(debounce_worker(queue, handler, delay=0.05))
    try:
        await asyncio.sleep(0.3)  # let the observer settle
        path = _wb(root) / "plan" / "nodes" / "node-b.md"
        path.write_text("---\nid: node-b\ntitle: B thing\ndepends_on: [node-a]\n---\n")
        await asyncio.wait_for(done.wait(), timeout=5)
        # Give the echo of our own hld.md/PLAN.md regeneration time to arrive.
        await asyncio.sleep(0.5)
    finally:
        worker.cancel()
        await stop_observer(observer)

    external = [(p, m) for p, m in results if m]
    assert len(external) == 1 and external[0][0].endswith("node-b.md")
    assert _types(external[0][1]) == ["plan.node.upsert", "plan.edge.upsert", "event.external"]
    assert store.get_node("node-b").depends_on == ["node-a"]
    assert "node-a --> node-b" in _hld_inner(root)
    # Every echo of our own regeneration writes was swallowed.
    assert all(not m for p, m in results if not p.endswith("node-b.md"))
