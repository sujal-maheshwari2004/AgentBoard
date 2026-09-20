"""The push-bullet vocabulary is one contract shared by three artefacts.

`docs/CONTRACTS.md` §9 carries the machine-readable registry; `skill/whiteboard/SKILL.md` tells the
root session what to do with each bullet; `docs/RUNBOOK.md` §3 tells the human. A prefix that is in
one and not the others is a silent protocol hole, which is how v1 shipped a `write_diagram`-only
skill. These tests keep all three (and the code that emits the bullets) in sync.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
CONTRACTS = (REPO / "docs" / "CONTRACTS.md").read_text(encoding="utf-8")
RUNBOOK = (REPO / "docs" / "RUNBOOK.md").read_text(encoding="utf-8")
SKILL = (REPO / "skill" / "whiteboard" / "SKILL.md").read_text(encoding="utf-8")
COLLABORATION = (REPO / "templates" / "COLLABORATION.md").read_text(encoding="utf-8")

#: Bullets whose emitter lands with the server stages (plan §A.2, §A.5, §A.6). Until those merge
#: the registry documents them and nothing pushes them; the set must only ever shrink.
PENDING_EMITTERS: set[str] = set()
# Every registry prefix has an emitter since A.S4:
#   "acknowledged plan edit (event " — report_progress(ack_event_seq=) (A.S3)
#   "diagram approved: " / "diagram rejected: " — handlers.on_diagram_reply
#   "agent plan edited: " / "agent diagram edited: " — handlers.on_agent_{plan,diagram}_edit
#   (the same two lines also come from the store's external-edit summaries)

#: v2 tools the root session is responsible for calling — they must be named in SKILL.md.
ROOT_V2_TOOLS = (
    "propose_diagram",
    "set_agent_ref",
    "report_progress",
    "finalize_agent",
    "chat_reply",
    "write_agent_diagram",
)

#: v2 tools a dispatched agent calls — they must be named in the collaboration protocol.
AGENT_V2_TOOLS = ("write_agent_plan", "write_agent_diagram", "report_progress", "chat_reply")


def _section(text: str, heading: str, level: str = "## ") -> str:
    """The text of one markdown section, up to the next heading of the same or higher level."""
    start = text.index(heading)
    body = text[start + len(heading):]
    stop = len(body)
    for line in re.finditer(r"^#{1,6} ", body, re.M):
        if len(line.group(0)) - 1 <= len(level.strip()):
            stop = line.start()
            break
    return body[:stop]


def bullet_prefixes() -> list[str]:
    """The `### Bullet prefix registry` list from CONTRACTS §9."""
    section = _section(CONTRACTS, "### Bullet prefix registry", level="### ")
    prefixes = [m.group(1) for m in re.finditer(r"^- `([^`]+)`\s*$", section, re.M)]
    assert prefixes, "the bullet prefix registry is empty or unparseable"
    return prefixes


PREFIXES = bullet_prefixes()


def _stem(prefix: str) -> str:
    """The part of a prefix that must appear verbatim in a source string literal."""
    return prefix.rstrip(" :(")


def _emitting_string_literals() -> list[str]:
    """Every string literal (including f-string text runs) in a module that calls `push_root`."""
    literals: list[str] = []
    for path in sorted((REPO / "whiteboard").rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        if "push_root" not in source:
            continue
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                literals.append(node.value)
    return literals


EMITTING_LITERALS = _emitting_string_literals()


def test_registry_has_the_18_documented_prefixes() -> None:
    assert len(PREFIXES) == 18
    assert len(set(PREFIXES)) == 18
    for expected in ("plan pasted", "chat (to: ", "diagram approved: ", "acknowledged plan edit (event "):
        assert expected in PREFIXES


@pytest.mark.parametrize("prefix", PREFIXES)
def test_skill_bullet_table_covers_every_prefix(prefix: str) -> None:
    table = _section(SKILL, "### Bullet → action", level="### ")
    assert prefix in table, f"SKILL.md 'Bullet → action' has no row for {prefix!r}"


@pytest.mark.parametrize("prefix", PREFIXES)
def test_runbook_section_3_covers_every_prefix(prefix: str) -> None:
    table = _section(RUNBOOK, "## 3. What arrives in the root session")
    assert prefix in table, f"RUNBOOK §3 has no row for {prefix!r}"


@pytest.mark.parametrize("prefix", PREFIXES)
def test_every_prefix_is_emitted_by_the_server(prefix: str) -> None:
    """A registry entry nothing can emit is dead protocol."""
    stem = _stem(prefix)
    emitted = any(stem in literal for literal in EMITTING_LITERALS)
    if not emitted:
        assert prefix in PENDING_EMITTERS, f"nothing under whiteboard/ can push {prefix!r}"


def test_pending_emitters_only_shrinks() -> None:
    """When a server stage lands its bullet, delete it from PENDING_EMITTERS."""
    still_pending = {
        prefix for prefix in PENDING_EMITTERS
        if not any(_stem(prefix) in literal for literal in EMITTING_LITERALS)
    }
    assert still_pending == PENDING_EMITTERS, (
        f"these bullets are emitted now; drop them from PENDING_EMITTERS: "
        f"{sorted(PENDING_EMITTERS - still_pending)}"
    )
    assert PENDING_EMITTERS <= set(PREFIXES)


@pytest.mark.parametrize("tool", ROOT_V2_TOOLS)
def test_skill_names_every_v2_root_tool(tool: str) -> None:
    assert tool in SKILL, f"SKILL.md never tells root about {tool}"


@pytest.mark.parametrize("tool", AGENT_V2_TOOLS)
def test_collaboration_names_every_v2_agent_tool(tool: str) -> None:
    assert tool in COLLABORATION, f"COLLABORATION.md never tells an agent about {tool}"


def test_skill_and_collaboration_cover_all_seven_new_tools() -> None:
    new_tools = {
        "propose_diagram", "set_agent_ref", "write_agent_plan",
        "write_agent_diagram", "report_progress", "finalize_agent", "chat_reply",
    }
    both = SKILL + COLLABORATION
    assert {t for t in new_tools if t not in both} == set()
