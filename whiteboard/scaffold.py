"""Create `.whiteboard/` and the project's Claude Code integration files.

Idempotent: existing files are never overwritten unless `force=True`; the
settings merge and gitignore append are safe to re-run.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

from whiteboard.agents_install import (
    AGENT_NAMES,
    installed_state,
    render_agent_definition,
)

TEMPLATES_DIR: Path = Path(__file__).resolve().parent.parent / "templates"

GITIGNORE_LINES: tuple[str, ...] = (
    ".whiteboard/server.json",
    ".whiteboard/server.log*",
    ".whiteboard/.cache/",
    ".whiteboard/**/*.layout.json",
)

# (destination relative to <root>, template name or None for an empty file)
_WHITEBOARD_FILES: tuple[tuple[str, str | None], ...] = (
    (".whiteboard/PLAN.md", "PLAN.md"),
    (".whiteboard/COLLABORATION.md", "COLLABORATION.md"),
    (".whiteboard/plan/hld.md", "hld.md"),
    (".whiteboard/plan/er.md", "er.md"),
    (".whiteboard/plan/nodes/.gitkeep", None),
    (".whiteboard/agents/.gitkeep", None),
    (".whiteboard/events.jsonl", None),
)


def _template(name: str) -> str:
    return (TEMPLATES_DIR / name).read_text(encoding="utf-8")


def _hook_template() -> dict:
    return json.loads(_template("hooks.json"))


def _hook_key(hook: dict) -> tuple:
    return (hook.get("type"), hook.get("command"), hook.get("matcher"))


def _merge_hook_event(existing: list, wanted: list) -> list:
    """Append hook groups whose command hooks are not already present."""
    present: set[tuple] = set()
    for group in existing:
        if isinstance(group, dict):
            for hook in group.get("hooks", []) or []:
                if isinstance(hook, dict):
                    present.add(_hook_key(hook))
    result = list(existing)
    for group in wanted:
        keys = {_hook_key(h) for h in group.get("hooks", [])}
        if keys and keys <= present:
            continue
        result.append(copy.deepcopy(group))
        present |= keys
    return result


def merge_settings(current: dict | None, template: dict) -> dict:
    """Deep-merge our hooks + crossSessionInbound into a settings dict.

    User keys, other hook events and other hook groups are preserved; our
    groups are appended only when absent, so repeated merges are a no-op.
    """
    merged = copy.deepcopy(current) if isinstance(current, dict) else {}
    for key, value in template.items():
        if key == "hooks":
            hooks = merged.get("hooks")
            if not isinstance(hooks, dict):
                hooks = {}
            for event, groups in value.items():
                existing = hooks.get(event)
                if not isinstance(existing, list):
                    existing = []
                hooks[event] = _merge_hook_event(existing, groups)
            merged["hooks"] = hooks
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def _load_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _write_file(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _scaffold_agent_definitions(
    root: Path, *, force: bool, created: list[str], skipped: list[str]
) -> dict[str, str]:
    """Write `<root>/.claude/agents/*.md` as stamped copies of the templates.

    Unlike the plain template files these are refreshed whenever the stamp's hash no longer
    matches (so a v1 project self-heals on re-scaffold), and a definition without a stamp is
    reported as `user-modified` and left exactly as it is unless `force`.
    """
    states: dict[str, str] = {}
    for name in AGENT_NAMES:
        rel = f".claude/agents/{name}.md"
        dest = root / rel
        wanted = render_agent_definition(name)
        state = installed_state(dest, wanted)
        if state == "unchanged" or (state == "user-modified" and not force):
            states[name] = "unchanged" if state == "unchanged" else "user-modified"
            skipped.append(rel)
            continue
        if dest.is_symlink():
            dest.unlink()
        _write_file(dest, wanted)
        states[name] = "installed" if state == "missing" else "refreshed"
        created.append(rel)
    return states


def scaffold(root: Path, *, force: bool = False) -> dict:
    """Populate `<root>/.whiteboard/` and `<root>/.claude/`.

    Returns `{created, skipped, agents, cross_session_inbound, settings_path}`.
    """
    root = Path(root).resolve()
    created: list[str] = []
    skipped: list[str] = []

    for rel, template_name in _WHITEBOARD_FILES:
        dest = root / rel
        if dest.exists() and not force:
            skipped.append(rel)
            continue
        _write_file(dest, _template(template_name) if template_name else "")
        created.append(rel)

    agents = _scaffold_agent_definitions(root, force=force, created=created, skipped=skipped)

    settings_path = root / ".claude" / "settings.local.json"
    settings_rel = ".claude/settings.local.json"
    current = _load_json(settings_path)
    merged = merge_settings(current, _hook_template())
    if current == merged:
        skipped.append(settings_rel)
    else:
        _write_file(settings_path, json.dumps(merged, indent=2) + "\n")
        created.append(settings_rel)

    gitignore = root / ".gitignore"
    existing_text = gitignore.read_text(encoding="utf-8") if gitignore.exists() else ""
    existing_lines = {line.strip() for line in existing_text.splitlines()}
    missing = [line for line in GITIGNORE_LINES if line not in existing_lines]
    if missing:
        prefix = "" if not existing_text or existing_text.endswith("\n") else "\n"
        block = prefix + "\n".join(missing) + "\n"
        gitignore.parent.mkdir(parents=True, exist_ok=True)
        with gitignore.open("a", encoding="utf-8") as fh:
            fh.write(block)
        created.append(".gitignore")
    else:
        skipped.append(".gitignore")

    return {
        "created": created,
        "skipped": skipped,
        "agents": agents,
        "cross_session_inbound": merged.get("crossSessionInbound"),
        "settings_path": str(settings_path),
    }
