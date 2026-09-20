import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from whiteboard.agents_install import (
    AGENT_NAMES,
    STAMP_PREFIX,
    TEMPLATES_DIR,
    install_agent_definitions,
    installed_state,
    render_agent_definition,
    source_text,
)

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture()
def templates(tmp_path: Path) -> Path:
    """A private copy of `templates/agents/` so a test can mutate a source template."""
    dest = tmp_path / "templates" / "agents"
    dest.mkdir(parents=True)
    for name in AGENT_NAMES:
        shutil.copyfile(TEMPLATES_DIR / "agents" / f"{name}.md", dest / f"{name}.md")
    return tmp_path / "templates"


def test_stamp_carries_the_source_hash() -> None:
    for name in AGENT_NAMES:
        template = source_text(name)
        rendered = render_agent_definition(name)
        digest = hashlib.sha256(template.encode("utf-8")).hexdigest()[:12]
        assert rendered.startswith(template.rstrip("\n"))
        last = rendered.rstrip("\n").splitlines()[-1]
        assert last.startswith(f"{STAMP_PREFIX}{name}.md; sha256={digest}.")
        assert last.endswith("-->")
        assert "make install-skill" in last
        # the stamp is a markdown comment, so the definition still parses as frontmatter + body
        assert rendered.startswith(f"---\nname: {name}\n")


def test_source_text_rejects_unknown_name() -> None:
    with pytest.raises(ValueError):
        source_text("whiteboard-nope")


def test_install_into_empty_dir_then_unchanged(tmp_path: Path) -> None:
    dest = tmp_path / "agents"
    first = install_agent_definitions(dest)
    assert first["ok"] is True
    assert first["dir"] == str(dest)
    assert {first[n] for n in AGENT_NAMES} == {"installed"}
    for name in AGENT_NAMES:
        path = dest / f"{name}.md"
        assert path.is_file() and not path.is_symlink()
        assert path.read_text() == render_agent_definition(name)

    second = install_agent_definitions(dest)
    assert second["ok"] is True
    assert {second[n] for n in AGENT_NAMES} == {"unchanged"}


def test_template_change_refreshes_the_copy(tmp_path: Path, templates: Path) -> None:
    dest = tmp_path / "agents"
    install_agent_definitions(dest, templates_dir=templates)
    src = templates / "agents" / "whiteboard-task.md"
    src.write_text(src.read_text() + "\n12. A brand new rule.\n")

    result = install_agent_definitions(dest, templates_dir=templates)
    assert result["ok"] is True
    assert result["whiteboard-task"] == "refreshed"
    assert result["whiteboard-liaison"] == "unchanged"
    assert "A brand new rule." in (dest / "whiteboard-task.md").read_text()
    assert (dest / "whiteboard-task.md").read_text() == render_agent_definition("whiteboard-task", templates)


def test_hand_edited_file_is_reported_and_left_alone(tmp_path: Path) -> None:
    dest = tmp_path / "agents"
    dest.mkdir()
    mine = dest / "whiteboard-task.md"
    mine.write_text("---\nname: whiteboard-task\n---\nmy own version\n")

    result = install_agent_definitions(dest)
    assert result["ok"] is False
    assert result["whiteboard-task"] == "user-modified"
    assert result["whiteboard-liaison"] == "installed"
    assert mine.read_text() == "---\nname: whiteboard-task\n---\nmy own version\n"


def test_force_overwrites_a_hand_edited_file(tmp_path: Path) -> None:
    dest = tmp_path / "agents"
    dest.mkdir()
    (dest / "whiteboard-task.md").write_text("mine\n")
    result = install_agent_definitions(dest, force=True)
    assert result["ok"] is True
    assert result["whiteboard-task"] == "refreshed"
    assert (dest / "whiteboard-task.md").read_text() == render_agent_definition("whiteboard-task")


def test_installed_state_transitions(tmp_path: Path, templates: Path) -> None:
    dest = tmp_path / "whiteboard-task.md"
    wanted = render_agent_definition("whiteboard-task", templates)
    assert installed_state(dest, wanted) == "missing"
    dest.write_text(wanted)
    assert installed_state(dest, wanted) == "unchanged"
    dest.write_text(wanted.replace("sha256=", "sha256=0"))
    assert installed_state(dest, wanted) == "stale"
    dest.write_text("no stamp here\n")
    assert installed_state(dest, wanted) == "user-modified"


def test_a_symlink_is_replaced_by_a_real_copy(tmp_path: Path) -> None:
    dest = tmp_path / "agents"
    dest.mkdir()
    link = dest / "whiteboard-task.md"
    link.symlink_to(TEMPLATES_DIR / "agents" / "whiteboard-task.md")
    result = install_agent_definitions(dest)
    assert result["whiteboard-task"] in ("installed", "refreshed")
    assert not link.is_symlink()
    assert link.read_text() == render_agent_definition("whiteboard-task")


def test_main_prints_json_and_exits_zero(tmp_path: Path) -> None:
    dest = tmp_path / "agents"
    proc = subprocess.run(
        [sys.executable, "-m", "whiteboard.agents_install", "--dest", str(dest)],
        capture_output=True, text=True, cwd=str(REPO), timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["ok"] is True and payload["dir"] == str(dest)
    assert {payload[n] for n in AGENT_NAMES} == {"installed"}

    (dest / "whiteboard-task.md").write_text("mine\n")
    again = subprocess.run(
        [sys.executable, "-m", "whiteboard.agents_install", "--dest", str(dest)],
        capture_output=True, text=True, cwd=str(REPO), timeout=60,
    )
    assert again.returncode == 0  # a user-modified file is a report, not a build failure
    assert json.loads(again.stdout)["whiteboard-task"] == "user-modified"
