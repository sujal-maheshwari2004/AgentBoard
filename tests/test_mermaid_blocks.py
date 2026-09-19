"""Tests for extract_mermaid_blocks / replace_mermaid_block."""

import pytest

from whiteboard.mermaid import extract_mermaid_blocks, parse, replace_mermaid_block

ONE_BLOCK = """# HLD

Some intro text.

```mermaid
flowchart TD
    node-files[Files layer]
    node-parser[Mermaid parser]
    node-files --> node-parser
```

Trailing prose.
"""

TWO_BLOCKS = """# Doc

```mermaid
flowchart TD
    A --> B
```

Middle text with a non-mermaid fence:

```python
print("not a diagram")
```

~~~mermaid
graph LR
    C --> D
~~~

End.
""".replace("~~~mermaid", "~~~mermaid" + " " * 3)  # trailing spaces after the info string


def test_extract_single_block() -> None:
    blocks = extract_mermaid_blocks(ONE_BLOCK)
    assert len(blocks) == 1
    start, end, inner = blocks[0]
    assert start == 4 and end == 9
    lines = ONE_BLOCK.split("\n")
    assert lines[start] == "```mermaid" and lines[end] == "```"
    assert inner == "flowchart TD\n    node-files[Files layer]\n    node-parser[Mermaid parser]\n    node-files --> node-parser\n"
    assert len(parse(inner).nodes) == 2


def test_extract_two_blocks_with_tilde_fence_and_trailing_spaces() -> None:
    blocks = extract_mermaid_blocks(TWO_BLOCKS)
    assert [(s, e) for s, e, _ in blocks] == [(2, 5), (13, 16)]
    lines = TWO_BLOCKS.split("\n")
    assert lines[13] == "~~~mermaid   " and lines[16] == "~~~"
    assert blocks[0][2] == "flowchart TD\n    A --> B\n"
    assert blocks[1][2] == "graph LR\n    C --> D\n"


def test_extract_ignores_other_fences_and_unclosed() -> None:
    assert extract_mermaid_blocks("```python\nx = 1\n```\n") == []
    assert extract_mermaid_blocks("```mermaid\nflowchart TD\n    A --> B\n") == []
    assert extract_mermaid_blocks("") == []
    assert extract_mermaid_blocks("no fences at all\n") == []


def test_extract_tolerates_indent_and_longer_fences() -> None:
    md = "  ````mermaid  \n  flowchart TD\n  ````\n"
    blocks = extract_mermaid_blocks(md)
    assert blocks == [(0, 2, "  flowchart TD\n")]
    # a shorter closing fence does not close a longer opening fence
    md2 = "````mermaid\nflowchart TD\n```\nstill inside\n````\n"
    assert extract_mermaid_blocks(md2) == [(0, 4, "flowchart TD\n```\nstill inside\n")]


def test_extract_empty_block() -> None:
    assert extract_mermaid_blocks("```mermaid\n```\n") == [(0, 1, "")]


def test_replace_first_block_preserves_rest() -> None:
    new = "flowchart TD\n    X --> Y\n"
    out = replace_mermaid_block(ONE_BLOCK, 0, new)
    assert out == ONE_BLOCK.replace(
        "flowchart TD\n    node-files[Files layer]\n    node-parser[Mermaid parser]\n    node-files --> node-parser\n",
        new,
    )
    assert extract_mermaid_blocks(out)[0][2] == new
    assert out.startswith("# HLD\n") and out.endswith("Trailing prose.\n")


def test_replace_second_of_two_blocks() -> None:
    out = replace_mermaid_block(TWO_BLOCKS, 1, "graph LR\n    C --> D --> E\n")
    blocks = extract_mermaid_blocks(out)
    assert blocks[0][2] == "flowchart TD\n    A --> B\n"
    assert blocks[1][2] == "graph LR\n    C --> D --> E\n"
    assert 'print("not a diagram")' in out
    assert out.endswith("~~~\n\nEnd.\n")


def test_replace_adds_missing_trailing_newline() -> None:
    out = replace_mermaid_block(ONE_BLOCK, 0, "flowchart TD\n    Q")
    assert extract_mermaid_blocks(out)[0][2] == "flowchart TD\n    Q\n"


def test_replace_appends_new_block_at_eof() -> None:
    md = "# Notes\n\nText.\n"
    out = replace_mermaid_block(md, 0, "flowchart TD\n    A --> B\n")
    assert out == "# Notes\n\nText.\n\n```mermaid\nflowchart TD\n    A --> B\n```\n"
    blocks = extract_mermaid_blocks(out)
    assert len(blocks) == 1 and blocks[0][2] == "flowchart TD\n    A --> B\n"
    # index == len(blocks) when blocks already exist appends a second block
    out2 = replace_mermaid_block(out, 1, "graph LR\n    C --> D\n")
    assert len(extract_mermaid_blocks(out2)) == 2
    assert out2.endswith("```\n\n```mermaid\ngraph LR\n    C --> D\n```\n")


def test_replace_appends_to_empty_or_unterminated_markdown() -> None:
    assert replace_mermaid_block("", 0, "flowchart TD\n") == "```mermaid\nflowchart TD\n```\n"
    out = replace_mermaid_block("no newline at end", 0, "flowchart TD\n")
    assert out == "no newline at end\n\n```mermaid\nflowchart TD\n```\n"


def test_replace_out_of_range() -> None:
    with pytest.raises(IndexError):
        replace_mermaid_block(ONE_BLOCK, 2, "flowchart TD\n")
    with pytest.raises(IndexError):
        replace_mermaid_block(ONE_BLOCK, -1, "flowchart TD\n")
