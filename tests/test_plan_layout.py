import json
from pathlib import Path

from whiteboard.plan.layout import (
    default_layout,
    edge_key,
    gc_layout,
    merge_layout,
    read_layout,
    write_layout,
)

SAMPLE = {
    "version": 1, "direction": "TD", "updatedAt": "x",
    "frames": {"plan-board": {"x": 0, "y": 0, "w": 1200, "h": 800, "collapsed": False}},
    "nodes": {"node-parser": {"x": 120, "y": 340, "w": 200, "h": 80, "parent": "plan-board", "pinned": True},
              "node-files": {"x": 0, "y": 0}},
    "agents": {"agent-parser": {"x": 1300, "y": 40, "w": 240, "h": 110}},
    "edges": {"node-files__node-parser": {"startAnchor": [0.5, 1], "endAnchor": [0.5, 0], "precise": False}},
}


def test_read_layout_missing_or_invalid(tmp_path: Path):
    assert read_layout(tmp_path / "nope.layout.json") == default_layout()
    p = tmp_path / "bad.layout.json"
    p.write_text("{not json")
    assert read_layout(p) == default_layout()
    p.write_text("[]")
    assert read_layout(p) == default_layout()
    p.write_text(json.dumps({"direction": "LR", "nodes": {"node-a": {"x": 1}}}))
    lay = read_layout(p)
    assert lay["direction"] == "LR" and lay["nodes"] == {"node-a": {"x": 1}}
    assert lay["frames"] == {} and lay["agents"] == {} and lay["edges"] == {} and lay["version"] == 1


def test_write_layout_default_writer_and_injected(tmp_path: Path):
    p = tmp_path / "plan" / "hld.layout.json"
    layout = json.loads(json.dumps(SAMPLE))
    write_layout(p, layout)
    on_disk = json.loads(p.read_text())
    assert on_disk["updatedAt"] != "x" and on_disk["updatedAt"].endswith("Z")
    assert layout["updatedAt"] == on_disk["updatedAt"]
    assert on_disk["nodes"] == SAMPLE["nodes"]
    assert read_layout(p) == on_disk

    calls: list[tuple[Path, bytes]] = []
    write_layout(tmp_path / "other.layout.json", layout, writer=lambda path, data: calls.append((path, data)))
    assert len(calls) == 1 and calls[0][0] == tmp_path / "other.layout.json"
    assert json.loads(calls[0][1])["direction"] == "TD"
    assert not (tmp_path / "other.layout.json").exists()


def test_merge_layout_deep_and_delete():
    merged = merge_layout(SAMPLE, {
        "nodes": {"node-parser": {"x": 999, "pinned": None}, "node-new": {"x": 1, "y": 2}, "node-files": None},
        "agents": {"agent-parser": {"y": 41}},
        "edges": {"node-files__node-parser": None, "node-a__node-b": {"precise": True}},
        "direction": "LR",
        "version": 99,
    })
    assert merged["nodes"]["node-parser"] == {"x": 999, "y": 340, "w": 200, "h": 80, "parent": "plan-board"}
    assert merged["nodes"]["node-new"] == {"x": 1, "y": 2}
    assert "node-files" not in merged["nodes"]
    assert merged["agents"]["agent-parser"] == {"x": 1300, "y": 41, "w": 240, "h": 110}
    assert merged["edges"] == {"node-a__node-b": {"precise": True}}
    assert merged["frames"] == SAMPLE["frames"]
    assert merged["direction"] == "LR" and merged["version"] == 1
    # inputs untouched
    assert SAMPLE["nodes"]["node-parser"]["x"] == 120 and "node-files" in SAMPLE["nodes"]
    assert merge_layout(SAMPLE, {}) == {**SAMPLE}
    assert merge_layout({}, {"nodes": {"node-a": {"x": 1}}})["nodes"] == {"node-a": {"x": 1}}


def test_gc_layout():
    out = gc_layout(SAMPLE, node_ids={"node-parser"}, agent_ids=set())
    assert set(out["nodes"]) == {"node-parser"}
    assert out["agents"] == {}
    assert out["edges"] == {}  # node-files is gone
    assert out["frames"] == SAMPLE["frames"]
    keep = gc_layout(SAMPLE, node_ids={"node-parser", "node-files"}, agent_ids={"agent-parser"})
    assert keep["nodes"] == SAMPLE["nodes"] and keep["edges"] == SAMPLE["edges"] and keep["agents"] == SAMPLE["agents"]
    assert edge_key("node-files", "node-parser") == "node-files__node-parser"
