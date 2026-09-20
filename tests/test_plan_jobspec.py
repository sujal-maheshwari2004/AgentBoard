from pathlib import Path

from whiteboard.plan.jobspec import PLAN_MD_NOTICE, render_job_spec, render_plan_md
from whiteboard.plan.model import AgentCard, Node


def nodes():
    files = Node(id="node-files", title="Files layer", status="done", interfaces=["atomic_write(path, data)"])
    parser = Node(id="node-parser", title="Mermaid parser", type="lld", depends_on=["node-files"],
                  interfaces=["parse(text) -> MermaidDoc", {"name": "serialize", "kind": "fn"}],
                  body="Parse flowcharts.\n\nKeep passthrough verbatim.\n", owner="agent-parser")
    store = Node(id="node-store", title="Plan store | v2", depends_on=["node-parser", "node-files"], status="blocked")
    return files, parser, store


def test_render_job_spec_sections_and_content():
    files, parser, store = nodes()
    md = render_job_spec(parser, deps=[files], dependents=[store], project_root=Path("/proj"))
    for heading in ["## Goal", "## Your node file path", "## Status protocol", "## Interfaces you must provide",
                    "## Interfaces you consume", "## Dependents waiting on you",
                    "## Diagrams you must produce", "## Scope rules", "## Read first"]:
        assert heading in md, heading
    assert md.index("## Goal") < md.index("## Your node file path") < md.index("## Status protocol")
    assert md.index("## Dependents waiting on you") < md.index("## Diagrams you must produce") < md.index("## Scope rules")
    assert "write_agent_diagram(agent_id=<your agent id>" in md and "write_agent_plan(agent_id=<your agent id>" in md
    assert "report_progress(agent_id=<your agent id>" in md and "box ids there are free-form" in md
    assert "Mermaid parser" in md and "Parse flowcharts." in md and "Keep passthrough verbatim." in md
    assert "`/proj/.whiteboard/plan/nodes/node-parser.md`" in md
    assert 'set_agent_status(agent_id=<your agent id>, status="working")' in md
    assert 'append_event(agent_id=<your agent id>, node_id="node-parser", type="done"' in md
    assert 'type="blocked"' in md and 'type="needs_input"' in md
    assert "ask_user(" in md and "get_reply(" in md
    assert "`parse(text) -> MermaidDoc`" in md and "`serialize (fn)`" in md
    assert "`node-files` — Files layer (status: done)" in md and "`atomic_write(path, data)`" in md
    assert "`node-store` — Plan store | v2 (status: blocked)" in md
    assert "/proj/.whiteboard/COLLABORATION.md" in md and "/proj/.whiteboard/PLAN.md" in md
    assert "Never edit other nodes' files" in md and "Only touch paths relevant" in md
    assert md.index("## Scope rules") < md.index("## Read first")


def test_render_job_spec_empty_lists_and_custom_paths():
    _, _, store = nodes()
    store = store.model_copy(update={"path": "custom/dir/node-store.md", "body": "   "})
    md = render_job_spec(store, deps=[], dependents=[], project_root=Path("/p"), collaboration_rel="docs/COLLAB.md")
    assert "`/p/custom/dir/node-store.md`" in md
    assert "/p/docs/COLLAB.md" in md
    assert "(no dependencies)" in md and "(none)" in md and "(none declared" in md


def test_render_plan_md():
    files, parser, store = nodes()
    agents = {"agent-parser": AgentCard(id="agent-parser", assigned_node="node-parser", status="working", ready_deps=["node-files"])}
    mermaid = "flowchart TD\n    node-files[Files layer]\n    node-parser[Mermaid parser]\n    node-files --> node-parser\n"
    md = render_plan_md({n.id: n for n in (files, parser, store)}, agents, mermaid)
    assert md.startswith("# PLAN\n\n" + PLAN_MD_NOTICE)
    assert "| id | title | type | status | owner | depends_on |" in md
    assert "| `node-parser` | Mermaid parser | lld | todo | `agent-parser` | `node-files` |" in md
    assert "| `node-store` | Plan store \\| v2 | hld | blocked | — | `node-parser`, `node-files` |" in md
    assert "| `agent-parser` | `node-parser` | working | `node-files` |" in md
    assert "```mermaid\n" + mermaid.rstrip("\n") + "\n```" in md
    assert "### Mermaid parser (`node-parser`)" in md
    assert "[`plan/nodes/node-parser.md`](plan/nodes/node-parser.md)" in md
    assert "[`plan/nodes/node-store.md`](plan/nodes/node-store.md)" in md
    assert "`serialize (fn)`" in md
    assert "Parse flowcharts." not in md  # bodies live in node files, not PLAN.md


def test_render_plan_md_boards():
    files, parser, store = nodes()
    hld = "flowchart TD\n    node-files[Files layer]\n    node-store[Plan store]\n"
    md = render_plan_md(
        {n.id: n for n in (files, parser, store)},
        {},
        "flowchart TD\n    node-files[Files layer]\n",
        boards={"hld": hld, "lld": "flowchart TD\n    node-parser[Mermaid parser]\n", "er": ""},
    )
    assert "## Boards" in md and md.index("## Flowchart") < md.index("## Boards") < md.index("## Node details")
    for name in ("HLD", "LLD", "ER"):
        assert f"### {name} (`plan/{name.lower()}.md`)" in md
    assert md.index("### HLD") < md.index("### LLD") < md.index("### ER")
    assert "```mermaid\n" + hld.rstrip("\n") + "\n```" in md
    # an empty board still renders a valid (empty) flowchart fence
    assert md[md.index("### ER"):].startswith("### ER (`plan/er.md`)\n\n```mermaid\nflowchart TD\n```\n")
    assert "(no boards)" not in md


def test_render_plan_md_empty():
    md = render_plan_md({}, {}, "")
    assert "(no nodes yet)" in md and "(no agents yet)" in md
    assert "```mermaid\nflowchart TD\n```" in md
    assert "## Boards" in md and "(no boards)" in md
