import json
from pathlib import Path

from whiteboard.agents_install import AGENT_NAMES, STAMP_PREFIX, render_agent_definition
from whiteboard.scaffold import GITIGNORE_LINES, TEMPLATES_DIR, merge_settings, scaffold

EXPECTED_FILES = [
    ".whiteboard/PLAN.md",
    ".whiteboard/COLLABORATION.md",
    ".whiteboard/plan/hld.md",
    ".whiteboard/plan/lld.md",
    ".whiteboard/plan/er.md",
    ".whiteboard/plan/nodes/.gitkeep",
    ".whiteboard/agents/.gitkeep",
    ".whiteboard/events.jsonl",
    ".claude/agents/whiteboard-task.md",
    ".claude/agents/whiteboard-liaison.md",
    ".claude/settings.local.json",
    ".gitignore",
]


def _settings(root: Path) -> dict:
    return json.loads((root / ".claude" / "settings.local.json").read_text())


def _hook_commands(settings: dict, event: str) -> list[str]:
    return [h["command"] for group in settings["hooks"][event] for h in group["hooks"]]


def test_fresh_scaffold_creates_everything(tmp_path: Path) -> None:
    result = scaffold(tmp_path)
    assert sorted(result["created"]) == sorted(EXPECTED_FILES)
    assert result["skipped"] == []
    for rel in EXPECTED_FILES:
        assert (tmp_path / rel).exists(), rel

    assert (tmp_path / ".whiteboard/events.jsonl").read_bytes() == b""
    assert "regenerated" in (tmp_path / ".whiteboard/PLAN.md").read_text()
    hld = (tmp_path / ".whiteboard/plan/hld.md").read_text()
    assert "```mermaid" in hld and "flowchart TD" in hld and "%% add nodes" in hld
    lld = (tmp_path / ".whiteboard/plan/lld.md").read_text()
    assert lld.startswith("# LLD")
    assert "```mermaid" in lld and "flowchart TD" in lld and "%% add nodes" in lld
    assert (tmp_path / ".whiteboard/plan/er.md").read_text().startswith("# ER")

    task = (tmp_path / ".claude/agents/whiteboard-task.md").read_text()
    assert task.startswith("---\nname: whiteboard-task\n")
    assert "mcp__whiteboard__*" in task and "disallowedTools: Agent, SendMessage" in task
    assert "maxTurns: 150" in task and "background: true" in task
    liaison = (tmp_path / ".claude/agents/whiteboard-liaison.md").read_text()
    assert liaison.startswith("---\nname: whiteboard-liaison\n")
    assert "tools: Read, mcp__whiteboard__*" in liaison
    assert "get_skeleton" in liaison and "read_peer_node" in liaison

    settings = _settings(tmp_path)
    assert settings["crossSessionInbound"] == "accept"
    assert _hook_commands(settings, "SessionStart") == ["bash ~/.claude/skills/whiteboard/scripts/session_hook.sh start"]
    assert _hook_commands(settings, "UserPromptSubmit") == ["bash ~/.claude/skills/whiteboard/scripts/session_hook.sh prompt"]

    gitignore = (tmp_path / ".gitignore").read_text()
    for line in GITIGNORE_LINES:
        assert gitignore.count(line + "\n") == 1


def test_rerun_skips_everything(tmp_path: Path) -> None:
    scaffold(tmp_path)
    (tmp_path / ".whiteboard/COLLABORATION.md").write_text("custom protocol\n")
    before = {rel: (tmp_path / rel).read_bytes() for rel in EXPECTED_FILES}
    result = scaffold(tmp_path)
    assert result["created"] == []
    assert sorted(result["skipped"]) == sorted(EXPECTED_FILES)
    for rel in EXPECTED_FILES:
        assert (tmp_path / rel).read_bytes() == before[rel], rel
    assert (tmp_path / ".whiteboard/COLLABORATION.md").read_text() == "custom protocol\n"


def test_force_overwrites_template_files_only(tmp_path: Path) -> None:
    scaffold(tmp_path)
    (tmp_path / ".whiteboard/COLLABORATION.md").write_text("custom protocol\n")
    (tmp_path / ".whiteboard/events.jsonl").write_text('{"seq": 1}\n')
    result = scaffold(tmp_path, force=True)
    assert ".whiteboard/COLLABORATION.md" in result["created"]
    assert (tmp_path / ".whiteboard/COLLABORATION.md").read_text() == (TEMPLATES_DIR / "COLLABORATION.md").read_text()
    # force re-creates the empty events file too (documented: force means template state)
    assert (tmp_path / ".whiteboard/events.jsonl").read_bytes() == b""
    # settings and gitignore are merges, still idempotent under force
    assert ".claude/settings.local.json" in result["skipped"]
    assert ".gitignore" in result["skipped"]


def test_settings_merge_preserves_user_content_and_is_idempotent(tmp_path: Path) -> None:
    settings_path = tmp_path / ".claude" / "settings.local.json"
    settings_path.parent.mkdir(parents=True)
    user = {
        "permissions": {"allow": ["Bash(git *)"]},
        "hooks": {
            "SessionStart": [{"hooks": [{"type": "command", "command": "echo hello"}]}],
            "Stop": [{"matcher": "", "hooks": [{"type": "command", "command": "say done"}]}],
        },
    }
    settings_path.write_text(json.dumps(user))

    first = scaffold(tmp_path)
    assert ".claude/settings.local.json" in first["created"]
    merged = _settings(tmp_path)
    assert merged["permissions"] == {"allow": ["Bash(git *)"]}
    assert merged["crossSessionInbound"] == "accept"
    assert _hook_commands(merged, "SessionStart") == [
        "echo hello",
        "bash ~/.claude/skills/whiteboard/scripts/session_hook.sh start",
    ]
    assert _hook_commands(merged, "Stop") == ["say done"]
    assert _hook_commands(merged, "UserPromptSubmit") == ["bash ~/.claude/skills/whiteboard/scripts/session_hook.sh prompt"]

    second = scaffold(tmp_path)
    assert ".claude/settings.local.json" in second["skipped"]
    assert _settings(tmp_path) == merged
    third = scaffold(tmp_path, force=True)
    assert ".claude/settings.local.json" in third["skipped"]
    assert _settings(tmp_path) == merged


def test_settings_merge_handles_invalid_json(tmp_path: Path) -> None:
    settings_path = tmp_path / ".claude" / "settings.local.json"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text("{broken")
    scaffold(tmp_path)
    assert _settings(tmp_path)["crossSessionInbound"] == "accept"


def test_merge_settings_unit() -> None:
    template = json.loads((TEMPLATES_DIR / "hooks.json").read_text())
    assert merge_settings(None, template) == template
    once = merge_settings({"hooks": "garbage"}, template)
    assert once["hooks"] == template["hooks"]
    assert merge_settings(once, template) == once


def test_scaffold_returns_agent_states_and_inbound(tmp_path: Path) -> None:
    result = scaffold(tmp_path)
    assert result["agents"] == {name: "installed" for name in AGENT_NAMES}
    assert result["cross_session_inbound"] == "accept"
    assert result["settings_path"] == str((tmp_path / ".claude" / "settings.local.json").resolve())
    assert _settings(tmp_path)["crossSessionInbound"] == "accept"

    again = scaffold(tmp_path)
    assert again["agents"] == {name: "unchanged" for name in AGENT_NAMES}
    assert again["cross_session_inbound"] == "accept"


def test_scaffolded_agent_definitions_are_stamped_copies(tmp_path: Path) -> None:
    scaffold(tmp_path)
    for name in AGENT_NAMES:
        path = tmp_path / ".claude" / "agents" / f"{name}.md"
        assert not path.is_symlink()
        assert path.read_text() == render_agent_definition(name)
        assert STAMP_PREFIX in path.read_text()


def test_rescaffold_refreshes_stale_agent_definition(tmp_path: Path) -> None:
    """A v1 project (or one built before a template change) self-heals on re-scaffold."""
    scaffold(tmp_path)
    path = tmp_path / ".claude" / "agents" / "whiteboard-task.md"
    stale = path.read_text().replace("sha256=", "sha256=0")
    path.write_text(stale)

    result = scaffold(tmp_path)  # no force
    assert result["agents"]["whiteboard-task"] == "refreshed"
    assert result["agents"]["whiteboard-liaison"] == "unchanged"
    assert ".claude/agents/whiteboard-task.md" in result["created"]
    assert ".claude/agents/whiteboard-liaison.md" in result["skipped"]
    assert path.read_text() == render_agent_definition("whiteboard-task")


def test_hand_edited_agent_definition_is_preserved(tmp_path: Path) -> None:
    scaffold(tmp_path)
    path = tmp_path / ".claude" / "agents" / "whiteboard-task.md"
    mine = "---\nname: whiteboard-task\n---\nmy own rules\n"
    path.write_text(mine)

    result = scaffold(tmp_path)
    assert result["agents"]["whiteboard-task"] == "user-modified"
    assert ".claude/agents/whiteboard-task.md" in result["skipped"]
    assert path.read_text() == mine

    forced = scaffold(tmp_path, force=True)
    assert forced["agents"]["whiteboard-task"] == "refreshed"
    assert path.read_text() == render_agent_definition("whiteboard-task")


def test_scaffolded_task_agent_mentions_v2_protocol(tmp_path: Path) -> None:
    scaffold(tmp_path)
    task = (tmp_path / ".claude" / "agents" / "whiteboard-task.md").read_text()
    for token in ("report_progress", "chat_reply", "write_agent_plan", "write_agent_diagram",
                  "ack_event_seq"):
        assert token in task, token
    assert "if the `mcp__whiteboard__*` tools are not available" in task.lower()
    liaison = (tmp_path / ".claude" / "agents" / "whiteboard-liaison.md").read_text()
    assert "chat_reply" in liaison and "report_progress" in liaison
    collab = (tmp_path / ".whiteboard" / "COLLABORATION.md").read_text()
    for token in ("## 4. Progress reporting", "## 6. Chat threads",
                  "## 7. Mid-run plan edits and acknowledgement", "write_agent_diagram"):
        assert token in collab, token


def test_gitignore_lines_appended_once_without_clobbering(tmp_path: Path) -> None:
    (tmp_path / ".gitignore").write_text("node_modules/\n.whiteboard/server.json")  # no trailing newline
    scaffold(tmp_path)
    text = (tmp_path / ".gitignore").read_text()
    assert text.startswith("node_modules/\n.whiteboard/server.json\n")
    for line in GITIGNORE_LINES:
        assert text.count(line + "\n") == 1
    scaffold(tmp_path)
    assert (tmp_path / ".gitignore").read_text() == text
