import json
from pathlib import Path

from whiteboard.scaffold import GITIGNORE_LINES, TEMPLATES_DIR, merge_settings, scaffold

EXPECTED_FILES = [
    ".whiteboard/PLAN.md",
    ".whiteboard/COLLABORATION.md",
    ".whiteboard/plan/hld.md",
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


def test_gitignore_lines_appended_once_without_clobbering(tmp_path: Path) -> None:
    (tmp_path / ".gitignore").write_text("node_modules/\n.whiteboard/server.json")  # no trailing newline
    scaffold(tmp_path)
    text = (tmp_path / ".gitignore").read_text()
    assert text.startswith("node_modules/\n.whiteboard/server.json\n")
    for line in GITIGNORE_LINES:
        assert text.count(line + "\n") == 1
    scaffold(tmp_path)
    assert (tmp_path / ".gitignore").read_text() == text
