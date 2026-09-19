"""Line-oriented parser for the Mermaid flowchart subset.

Grammar covered (see docs/research/tldraw-mermaid.md, "Mermaid flowchart subset"):

* header ``flowchart|graph`` + ``TB|TD|BT|LR|RL``
* node tokens ``id``, ``id[Label]``, ``id(Label)``, ``id([Label])``, ``id[[Label]]``,
  ``id[(Label)]``, ``id((Label))``, ``id(((Label)))``, ``id>Label]``, ``id{Label}``,
  ``id{{Label}}``, ``id[/L/]``, ``id[\\L\\]``, ``id[/L\\]``, ``id[\\L/]``, quoted labels,
  ``:::class`` suffixes
* links ``-->``, ``---``, ``-.->``, ``-.-``, ``==>``, ``===``, ``--o``, ``--x`` (+ longer
  variants, ``<`` bidirectional prefix treated as a plain arrow), pipe labels
  ``-->|text|`` and inline labels ``-- text -->`` / ``-. text .->`` / ``== text ==>``
* chains ``A --> B --> C`` and groups ``A & B --> C & D``
* ``subgraph`` ... ``end`` with optional ``direction``; nesting supported
* ``%%`` comments, blank lines, ``style``/``classDef``/``class``/``linkStyle``/
  ``click``/``direction`` (outside a subgraph)/``accTitle``/``accDescr`` and
  ``id@{ ... }`` lines are kept verbatim as :class:`Passthrough`
* ``;`` separates statements on one line (outside quotes and brackets)
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .ast import Edge, MermaidDoc, MermaidError, Node, Passthrough, Subgraph

DIRECTIONS = ("TB", "TD", "BT", "LR", "RL")

# ``-`` is allowed inside ids but must not start a link operator.
ID = r"[A-Za-z0-9_](?:[A-Za-z0-9_.]|-(?![-.=]))*"
ID_RE = re.compile(rf"^{ID}$")

HEADER_RE = re.compile(r"^(?P<kind>flowchart|graph)(?:\s+(?P<dir>TB|TD|BT|LR|RL))?\s*$")
PASSTHROUGH_RE = re.compile(
    r"^(?:(?:style|classDef|class|linkStyle|click)\s|direction\s+(?:TB|TD|BT|LR|RL)\s*$"
    r"|accTitle\s*:|accDescr\s*[:{]"
    rf"|{ID}\s*@\{{)"
)
DIRECTION_RE = re.compile(r"^direction\s+(?P<dir>TB|TD|BT|LR|RL)\s*$")
SUBGRAPH_RE = re.compile(r"^subgraph(?:\s+(?P<rest>.*?))?\s*$")
SUBGRAPH_ID_TITLE_RE = re.compile(rf"^(?P<id>{ID})\s*\[(?P<title>.*)\]$")
QUOTED_RE = re.compile(r'^"(?P<inner>[^"]*)"$')

# Alternation order is load-bearing: longer/more specific delimiters first.
_SHAPES: tuple[tuple[str, str, str, str], ...] = (
    # (group name, shape, open, close)
    ("dcircle", "double-circle", "(((", ")))"),
    ("circle", "circle", "((", "))"),
    ("stadium", "stadium", "([", "])"),
    ("cylinder", "cylinder", "[(", ")]"),
    ("subroutine", "subroutine", "[[", "]]"),
    ("hexagon", "hexagon", "{{", "}}"),
    ("para", "parallelogram", "[/", "/]"),
    ("paraalt", "parallelogram-alt", "[\\", "\\]"),
    ("trap", "trapezoid", "[/", "\\]"),
    ("trapalt", "trapezoid-alt", "[\\", "/]"),
    ("asym", "asymmetric", ">", "]"),
    ("rect", "rect", "[", "]"),
    ("round", "round", "(", ")"),
    ("rhombus", "rhombus", "{", "}"),
)
_GROUP_TO_SHAPE = {g: s for g, s, _, _ in _SHAPES}
SHAPE_DELIMS: dict[str, tuple[str, str]] = {s: (o, c) for _, s, o, c in _SHAPES}


def _body(group: str, open_: str, close: str) -> str:
    return (
        rf"{re.escape(open_)}"
        rf'(?:\s*"(?P<q_{group}>[^"]*)"\s*|(?P<u_{group}>.*?))'
        rf"{re.escape(close)}"
    )


SHAPE_ALT = "|".join(_body(g, o, c) for g, _, o, c in _SHAPES)
NODE_RE = re.compile(rf"(?P<id>{ID})(?P<shape>{SHAPE_ALT})?(?P<cls>(?::::[A-Za-z0-9_-]+)+)?")

LINK_RE = re.compile(
    r"(?P<bidir><)?"
    r"(?:(?P<solid>-{2,}[>ox]|-{3,})"
    r"|(?P<dotted>-\.+-[>ox]?)"
    r"|(?P<thick>={2,}[>ox]|={3,}))"
    r"(?:\s*\|(?P<plabel>[^|]*)\|)?"
)
INLINE_SOLID_RE = re.compile(r"<?--\s*(?P<label>\S.*?)\s*(?P<close>-{2,}[>ox]|-{3,})")
INLINE_DOTTED_RE = re.compile(r"<?-\.\s*(?P<label>\S.*?)\s*(?P<close>\.+-[>ox]|\.+-)")
INLINE_THICK_RE = re.compile(r"<?==\s*(?P<label>\S.*?)\s*(?P<close>={2,}[>ox]|={3,})")

_HEADS = {">": "arrow", "o": "circle", "x": "cross"}


def decode_label(text: str) -> str:
    """Decode the entity codes the serializer emits."""
    return text.replace("#quot;", '"')


def _unquote(text: str) -> tuple[str, bool]:
    text = text.strip()
    m = QUOTED_RE.match(text)
    if m:
        return decode_label(m.group("inner")), True
    return decode_label(text), False


@dataclass
class _NodeTok:
    id: str
    label: str | None
    shape: str
    quoted: bool
    classes: list[str]


@dataclass
class _LinkTok:
    style: str
    head: str
    length: int
    label: str | None
    label_form: str


def split_statements(line: str) -> list[str]:
    """Split on ``;`` outside quotes, brackets and ``|...|`` labels; drops empty pieces."""
    pieces: list[str] = []
    buf: list[str] = []
    depth = 0
    in_quote = False
    in_pipe = False
    for ch in line:
        if ch == '"':
            in_quote = not in_quote
        elif not in_quote:
            if ch == "|" and depth == 0:
                in_pipe = not in_pipe
            elif ch in "[({":
                depth += 1
            elif ch in "])}":
                depth = max(0, depth - 1)
            elif ch == ";" and depth == 0 and not in_pipe:
                pieces.append("".join(buf))
                buf = []
                continue
        buf.append(ch)
    pieces.append("".join(buf))
    return [p.strip() for p in pieces if p.strip()]


def _link_from_plain(m: re.Match[str]) -> _LinkTok:
    label = m.group("plabel")
    if m.group("solid") is not None:
        op = m.group("solid")
        style = "solid"
        head = _HEADS.get(op[-1], "none")
        length = op.count("-") - (1 if head != "none" else 2)
    elif m.group("dotted") is not None:
        op = m.group("dotted")
        style = "dotted"
        head = _HEADS.get(op[-1], "none")
        length = op.count(".")
    else:
        op = m.group("thick")
        style = "thick"
        head = _HEADS.get(op[-1], "none")
        length = op.count("=") - (1 if head != "none" else 2)
    lab: str | None = None
    if label is not None:
        lab, _ = _unquote(label)
    return _LinkTok(style, head, max(1, length), lab, "pipe")


def _link_from_inline(style: str, m: re.Match[str]) -> _LinkTok:
    close = m.group("close")
    head = _HEADS.get(close[-1], "none")
    if style == "solid":
        length = close.count("-") - (1 if head != "none" else 2)
    elif style == "dotted":
        length = close.count(".")
    else:
        length = close.count("=") - (1 if head != "none" else 2)
    label, _ = _unquote(m.group("label"))
    return _LinkTok(style, head, max(1, length), label, "inline")


def _node_from_match(m: re.Match[str]) -> _NodeTok:
    label: str | None = None
    shape = "bare"
    quoted = False
    if m.group("shape") is not None:
        for group in _GROUP_TO_SHAPE:
            q = m.group(f"q_{group}")
            u = m.group(f"u_{group}")
            if q is not None:
                shape, label, quoted = _GROUP_TO_SHAPE[group], decode_label(q), True
                break
            if u is not None:
                shape = _GROUP_TO_SHAPE[group]
                label, quoted = _unquote(u)
                break
    classes = [c for c in (m.group("cls") or "").split(":::") if c]
    return _NodeTok(m.group("id"), label, shape, quoted, classes)


def tokenize_statement(text: str) -> tuple[list[list[_NodeTok]], list[_LinkTok]]:
    """Return alternating node groups and links for one statement.

    Raises ``ValueError`` with a message (no line info) on unknown syntax.
    """
    groups: list[list[_NodeTok]] = []
    links: list[_LinkTok] = []
    pos = 0
    n = len(text)
    while True:
        # node group
        group: list[_NodeTok] = []
        while True:
            while pos < n and text[pos].isspace():
                pos += 1
            m = NODE_RE.match(text, pos)
            if not m:
                raise ValueError(f"expected a node at column {pos + 1}")
            group.append(_node_from_match(m))
            pos = m.end()
            save = pos
            while pos < n and text[pos].isspace():
                pos += 1
            if pos < n and text[pos] == "&":
                pos += 1
                continue
            pos = save
            break
        groups.append(group)
        while pos < n and text[pos].isspace():
            pos += 1
        if pos >= n:
            break
        m = LINK_RE.match(text, pos)
        if m:
            links.append(_link_from_plain(m))
            pos = m.end()
            continue
        for style, rx in (
            ("solid", INLINE_SOLID_RE),
            ("dotted", INLINE_DOTTED_RE),
            ("thick", INLINE_THICK_RE),
        ):
            m = rx.match(text, pos)
            if m:
                links.append(_link_from_inline(style, m))
                pos = m.end()
                break
        else:
            raise ValueError(f"expected a link operator at column {pos + 1}")
    return groups, links


class _Parser:
    def __init__(self, text: str) -> None:
        self.lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        self.doc = MermaidDoc()
        self.header_seen = False
        self.serial = -1  # last statement serial handed out
        self.stack: list[int] = []  # indices into doc.subgraphs
        self.sg_line: list[int] = []
        self.indent: str | None = None
        self.pending_blank: list[Passthrough] = []

    # -- helpers ---------------------------------------------------------
    def _next(self) -> int:
        self.serial += 1
        return self.serial

    def _passthrough(self, text: str) -> None:
        p = Passthrough(text, self.serial, before_header=not self.header_seen)
        if text == "":
            # Blank lines are kept only if a statement follows (see flush).
            self.pending_blank.append(p)
            return
        self._flush_blank()
        self.doc.passthrough.append(p)

    def _flush_blank(self) -> None:
        if self.pending_blank:
            if self.header_seen:
                self.doc.passthrough.extend(self.pending_blank)
            self.pending_blank = []

    def _register(self, tok: _NodeTok, serial: int) -> Node:
        node = self.doc.nodes.get(tok.id)
        if node is None:
            node = Node(tok.id, tok.label, tok.shape, list(tok.classes), tok.quoted, serial)  # type: ignore[arg-type]
            self.doc.nodes[tok.id] = node
        else:
            if tok.shape != "bare":
                node.label, node.shape, node.quoted = tok.label, tok.shape, tok.quoted  # type: ignore[assignment]
            for c in tok.classes:
                if c not in node.classes:
                    node.classes.append(c)
        if self.stack and self.doc.subgraph_of(tok.id) is None:
            self.doc.subgraphs[self.stack[-1]].members.append(tok.id)
        return node

    # -- driver ----------------------------------------------------------
    def run(self) -> MermaidDoc:
        i = 0
        total = len(self.lines)
        while i < total:
            idx = i + 1  # 1-based for error reporting
            raw = self.lines[i]
            i += 1
            stripped = raw.strip()
            if stripped == "":
                self._passthrough("")
                continue
            if stripped.startswith("%%"):
                self._passthrough(stripped)
                continue
            if not self.header_seen and stripped == "---":
                # YAML frontmatter block before the header: keep verbatim
                # (including its closing ``---``) above the header.
                self._passthrough("---")
                while i < total and self.lines[i].strip() != "---":
                    self._passthrough(self.lines[i].rstrip())
                    i += 1
                if i < total:
                    self._passthrough("---")
                    i += 1
                continue
            if self.header_seen and self.indent is None:
                self.indent = raw[: len(raw) - len(raw.lstrip())]
            for stmt in split_statements(stripped):
                if not self.header_seen:
                    m = HEADER_RE.match(stmt)
                    if not m:
                        raise MermaidError("expected 'flowchart <dir>' or 'graph <dir>' header", idx, raw)
                    self.doc.kind = m.group("kind")  # type: ignore[assignment]
                    self.doc.direction = m.group("dir") or "TD"
                    self.header_seen = True
                    self.pending_blank = []
                    continue
                self._statement(stmt, idx, raw)
        if not self.header_seen:
            raise MermaidError("missing 'flowchart <dir>' or 'graph <dir>' header", total or 1, "")
        if self.stack:
            line = self.sg_line[-1]
            raise MermaidError("subgraph without matching 'end'", line, self.lines[line - 1])
        self.pending_blank = []  # trailing blank lines are dropped
        if self.indent is not None:
            self.doc.indent = self.indent
        return self.doc

    def _statement(self, stmt: str, idx: int, raw: str) -> None:
        m = DIRECTION_RE.match(stmt)
        if m and self.stack:
            self.doc.subgraphs[self.stack[-1]].direction = m.group("dir")
            return
        if PASSTHROUGH_RE.match(stmt):
            self._passthrough(stmt)
            return
        if stmt == "end":
            if not self.stack:
                raise MermaidError("'end' without an open subgraph", idx, raw)
            self.stack.pop()
            self.sg_line.pop()
            return
        m = SUBGRAPH_RE.match(stmt)
        if m:
            self._flush_blank()
            self._open_subgraph(m.group("rest") or "", idx, raw)
            return
        try:
            groups, links = tokenize_statement(stmt)
        except ValueError as exc:
            raise MermaidError(str(exc), idx, raw) from None
        self._flush_blank()
        if not links:
            for tok in groups[0]:
                self._register(tok, self._next())
            return
        first = self.serial + 1
        for group in groups:
            for tok in group:
                self._register(tok, first)
        for i, link in enumerate(links):
            for src in groups[i]:
                for dst in groups[i + 1]:
                    self.doc.edges.append(
                        Edge(
                            src.id,
                            dst.id,
                            link.label,
                            link.style,  # type: ignore[arg-type]
                            link.head,  # type: ignore[arg-type]
                            link.length,
                            link.label_form,  # type: ignore[arg-type]
                            self._next(),
                        )
                    )

    def _open_subgraph(self, rest: str, idx: int, raw: str) -> None:
        rest = rest.strip()
        sg_id: str | None = None
        title: str | None = None
        if not rest:
            raise MermaidError("subgraph needs an id or title", idx, raw)
        m = SUBGRAPH_ID_TITLE_RE.match(rest)
        if m:
            sg_id = m.group("id")
            title, _ = _unquote(m.group("title"))
        else:
            q = QUOTED_RE.match(rest)
            if q:
                title = decode_label(q.group("inner"))
            elif ID_RE.match(rest):
                sg_id = rest
            else:
                title = decode_label(rest)
        parent = self.stack[-1] if self.stack else None
        sg = Subgraph(sg_id, title, None, [], len(self.doc.subgraphs), parent)
        self.doc.subgraphs.append(sg)
        self.stack.append(len(self.doc.subgraphs) - 1)
        self.sg_line.append(idx)


def parse(text: str) -> MermaidDoc:
    """Parse flowchart text; raises :class:`MermaidError` with a 1-based line."""
    return _Parser(text).run()


__all__ = ["parse", "split_statements", "tokenize_statement", "decode_label", "SHAPE_DELIMS"]
