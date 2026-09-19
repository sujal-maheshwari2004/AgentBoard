"""Mermaid flowchart subset: parser, serializer, AST and markdown fence helpers."""

from __future__ import annotations

import re

from .ast import Edge, MermaidDoc, MermaidError, Node, Passthrough, Subgraph
from .parser import parse
from .serializer import serialize

_FENCE_OPEN_RE = re.compile(r"^\s*(?P<fence>`{3,}|~{3,})\s*mermaid(?:\s.*)?$")


def _closes(line: str, fence: str) -> bool:
    s = line.strip()
    return bool(s) and set(s) == {fence[0]} and len(s) >= len(fence)


def extract_mermaid_blocks(markdown: str) -> list[tuple[int, int, str]]:
    """Return ``(fence_start_line_idx, fence_end_line_idx, inner_text)`` per block.

    Line indices are 0-based into ``markdown.split("\\n")``.  ``inner_text`` is
    the lines between the fences joined with ``\\n`` plus a trailing newline
    (empty string for an empty block).  Unclosed fences are ignored.
    """
    lines = markdown.split("\n")
    blocks: list[tuple[int, int, str]] = []
    i = 0
    while i < len(lines):
        m = _FENCE_OPEN_RE.match(lines[i])
        if not m:
            i += 1
            continue
        fence = m.group("fence")
        j = i + 1
        while j < len(lines) and not _closes(lines[j], fence):
            j += 1
        if j >= len(lines):
            break
        inner_lines = lines[i + 1 : j]
        inner = "\n".join(inner_lines) + "\n" if inner_lines else ""
        blocks.append((i, j, inner))
        i = j + 1
    return blocks


def replace_mermaid_block(markdown: str, index: int, new_inner: str) -> str:
    """Replace the ``index``-th block's inner text, or append a new block when
    ``index == len(blocks)``."""
    blocks = extract_mermaid_blocks(markdown)
    if new_inner and not new_inner.endswith("\n"):
        new_inner += "\n"
    if index == len(blocks):
        out = markdown
        if out and not out.endswith("\n"):
            out += "\n"
        if out:
            out += "\n"
        return out + "```mermaid\n" + new_inner + "```\n"
    if index < 0 or index > len(blocks):
        raise IndexError(f"no mermaid block at index {index} (found {len(blocks)})")
    start, end, _ = blocks[index]
    lines = markdown.split("\n")
    inner_lines = new_inner[:-1].split("\n") if new_inner else []
    return "\n".join(lines[: start + 1] + inner_lines + lines[end:])


__all__ = [
    "Edge",
    "MermaidDoc",
    "MermaidError",
    "Node",
    "Passthrough",
    "Subgraph",
    "extract_mermaid_blocks",
    "parse",
    "replace_mermaid_block",
    "serialize",
]
