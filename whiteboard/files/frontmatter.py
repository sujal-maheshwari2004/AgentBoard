"""YAML frontmatter split/join that keeps key order and the body byte-exact.

``python-frontmatter`` alphabetises keys and strips the body, which turns every
save into a whole-file diff; this module is the replacement (CONTRACTS §2).
"""

from __future__ import annotations

import yaml

__all__ = ["split_frontmatter", "join_frontmatter", "order_keys"]

_FENCE = "---"


def split_frontmatter(text: str) -> tuple[dict, str]:
    """Split ``text`` into ``(meta, body)``.

    Returns ``({}, text)`` when there is no leading ``---`` block, when the YAML
    is invalid, or when it does not parse to a mapping. On success the body is
    everything after the closing ``---`` line (byte-exact, leading blank lines
    and trailing whitespace included).
    """
    lines = text.split("\n")
    if not lines or lines[0].rstrip("\r") != _FENCE:
        return {}, text
    for i in range(1, len(lines)):
        if lines[i].rstrip("\r") == _FENCE:
            yaml_text = "\n".join(lines[1:i])
            try:
                meta = yaml.safe_load(yaml_text)
            except yaml.YAMLError:
                return {}, text
            if meta is None:
                meta = {}
            if not isinstance(meta, dict):
                return {}, text
            # Body starts right after the closing fence line and its newline.
            consumed = sum(len(line) + 1 for line in lines[: i + 1])
            return meta, text[consumed:]
    return {}, text


def join_frontmatter(meta: dict, body: str) -> str:
    """Render ``---\\n<yaml>---\\n\\n<body>`` with insertion-ordered keys.

    Leading newlines are stripped from ``body`` so the blank separator line is
    always exactly one; the rest of the body is emitted unchanged. ``None``
    values dump as ``null``.
    """
    dumped = yaml.safe_dump(
        dict(meta),
        sort_keys=False,
        default_flow_style=False,
        allow_unicode=True,
        width=100,
        indent=2,
    )
    if dumped == "{}\n":
        dumped = ""
    elif not dumped.endswith("\n"):
        dumped += "\n"
    return f"{_FENCE}\n{dumped}{_FENCE}\n\n{body.lstrip('\n')}"


def order_keys(meta: dict, first: list[str]) -> dict:
    """Return a new dict with ``first`` keys (those present) in that order, then
    the remaining keys in their original order."""
    out: dict = {}
    for key in first:
        if key in meta:
            out[key] = meta[key]
    for key, value in meta.items():
        if key not in out:
            out[key] = value
    return out
