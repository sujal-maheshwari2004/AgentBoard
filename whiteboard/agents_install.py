"""Install the two whiteboard agent definitions as *copies* with a provenance stamp.

`~/.claude/agents/*.md` is parsed once at session start. A symlink that dangles (the repo moved,
was renamed or is on an unmounted volume) makes `subagent_type: whiteboard-task` fail silently at
dispatch time, mid-run; a copy always resolves. The stamp carries the source hash, so a refresh is
deterministic and a hand-edited file (no stamp) is reported rather than clobbered.

Pure and unit-testable: `enter.sh`, `make install-agents` and `whiteboard.scaffold` all call it.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

TEMPLATES_DIR: Path = Path(__file__).resolve().parent.parent / "templates"

AGENT_NAMES: tuple[str, ...] = ("whiteboard-task", "whiteboard-liaison")

STAMP_PREFIX = "<!-- installed by AgentBoard from templates/agents/"

#: states `installed_state` can report for a file on disk
MISSING = "missing"
UNCHANGED = "unchanged"
STALE = "stale"
USER_MODIFIED = "user-modified"


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def source_text(name: str, templates_dir: Path | None = None) -> str:
    """The raw template for `name` (no stamp)."""
    if name not in AGENT_NAMES:
        raise ValueError(f"unknown agent definition: {name}")
    base = Path(templates_dir) if templates_dir else TEMPLATES_DIR
    return (base / "agents" / f"{name}.md").read_text(encoding="utf-8")


def stamp_for(name: str, template: str) -> str:
    return (
        f"{STAMP_PREFIX}{name}.md; sha256={_digest(template)}. "
        "Re-run `make install-skill` or the whiteboard skill to refresh; "
        "edit the template, not this file. -->"
    )


def render_agent_definition(name: str, templates_dir: Path | None = None) -> str:
    """The template plus the provenance stamp as its last line."""
    template = source_text(name, templates_dir)
    return template.rstrip("\n") + "\n\n" + stamp_for(name, template) + "\n"


def installed_state(dest: Path, wanted: str) -> str:
    """`missing` | `unchanged` | `stale` | `user-modified` for the file at `dest`."""
    dest = Path(dest)
    if dest.is_symlink():
        # Never ours and never acceptable: a symlink is exactly the failure mode this module
        # exists to remove, so it is always replaced by a real copy.
        return STALE
    try:
        current = dest.read_text(encoding="utf-8")
    except (FileNotFoundError, NotADirectoryError, IsADirectoryError, UnicodeDecodeError, OSError):
        return MISSING
    if STAMP_PREFIX not in current:
        return USER_MODIFIED
    if current == wanted:
        return UNCHANGED
    return STALE


def install_agent_definitions(
    dest_dir: Path | str,
    *,
    templates_dir: Path | None = None,
    force: bool = False,
) -> dict:
    """Copy both definitions into `dest_dir`; never follows or creates symlinks.

    Returns `{ok, dir, "whiteboard-task": state, "whiteboard-liaison": state}` where each state is
    one of `installed | refreshed | unchanged | user-modified | failed:<err>`, and `ok` is true
    when every state is in `{installed, refreshed, unchanged}`.
    """
    dest_dir = Path(dest_dir)
    result: dict = {"ok": True, "dir": str(dest_dir)}
    for name in AGENT_NAMES:
        dest = dest_dir / f"{name}.md"
        try:
            wanted = render_agent_definition(name, templates_dir)
            state = installed_state(dest, wanted)
            if state == UNCHANGED:
                outcome = UNCHANGED
            elif state == USER_MODIFIED and not force:
                outcome = USER_MODIFIED
            else:
                dest_dir.mkdir(parents=True, exist_ok=True)
                if dest.is_symlink():
                    dest.unlink()
                dest.write_text(wanted, encoding="utf-8")
                outcome = "installed" if state == MISSING else "refreshed"
        except Exception as exc:  # pragma: no cover - defensive; never blocks entry
            outcome = f"failed:{exc}"
        result[name] = outcome
        if outcome not in ("installed", "refreshed", UNCHANGED):
            result["ok"] = False
    return result


def _main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="python -m whiteboard.agents_install")
    parser.add_argument("--dest", default=str(Path.home() / ".claude" / "agents"))
    parser.add_argument("--force", action="store_true", help="overwrite hand-edited definitions")
    args = parser.parse_args(argv)
    result = install_agent_definitions(Path(args.dest), force=args.force)
    # Always exit 0: a `user-modified` definition is a report, not a build failure, and
    # `make install-skill` / `enter.sh` must not abort on one.
    print(json.dumps(result))
    return 0


__all__ = [
    "AGENT_NAMES",
    "STAMP_PREFIX",
    "TEMPLATES_DIR",
    "install_agent_definitions",
    "installed_state",
    "render_agent_definition",
    "source_text",
    "stamp_for",
]


if __name__ == "__main__":  # pragma: no cover - exercised via subprocess in tests
    raise SystemExit(_main())
