from whiteboard.files import join_frontmatter, order_keys, split_frontmatter

NODE_ORDER = ["id", "type", "title", "status", "owner", "depends_on", "interfaces"]


def test_split_basic() -> None:
    text = "---\nid: node-a\ntitle: A\n---\n\nBody here.\n"
    meta, body = split_frontmatter(text)
    assert meta == {"id": "node-a", "title": "A"}
    assert body == "\nBody here.\n"


def test_split_absent() -> None:
    assert split_frontmatter("# Just markdown\n") == ({}, "# Just markdown\n")
    assert split_frontmatter("") == ({}, "")
    assert split_frontmatter("--- \nnot a fence\n") == ({}, "--- \nnot a fence\n")


def test_split_unterminated_returns_whole_text() -> None:
    text = "---\nid: x\nno closing fence\n"
    assert split_frontmatter(text) == ({}, text)


def test_split_malformed_yaml() -> None:
    text = "---\nid: [unclosed\ntitle: : :\n---\n\nbody\n"
    assert split_frontmatter(text) == ({}, text)


def test_split_non_mapping_yaml() -> None:
    text = "---\n- a\n- b\n---\n\nbody\n"
    assert split_frontmatter(text) == ({}, text)
    text2 = "---\njust a string\n---\nbody"
    assert split_frontmatter(text2) == ({}, text2)


def test_split_empty_frontmatter() -> None:
    meta, body = split_frontmatter("---\n---\nbody")
    assert meta == {}
    assert body == "body"


def test_split_body_byte_exact_leading_blank_and_trailing_ws() -> None:
    body_in = "\n\n\n  indented first line\nline two   \n\n\t\n   "
    text = "---\nid: node-a\n---\n" + body_in
    meta, body = split_frontmatter(text)
    assert meta == {"id": "node-a"}
    assert body == body_in


def test_split_body_with_mermaid_fence_and_dashes() -> None:
    body_in = "```mermaid\nflowchart TD\n    a --> b\n```\n\n---\n\nhorizontal rule above\n"
    text = "---\nid: node-a\n---\n" + body_in
    _, body = split_frontmatter(text)
    assert body == body_in


def test_split_crlf_fences() -> None:
    text = "---\r\nid: node-a\r\n---\r\nbody\r\n"
    meta, body = split_frontmatter(text)
    assert meta == {"id": "node-a"}
    assert body == "body\r\n"


def test_join_format_and_null() -> None:
    meta = {"id": "node-a", "owner": None, "depends_on": []}
    out = join_frontmatter(meta, "Body\n")
    assert out == "---\nid: node-a\nowner: null\ndepends_on: []\n---\n\nBody\n"


def test_join_strips_leading_newlines_only() -> None:
    out = join_frontmatter({"id": "x"}, "\n\n\nBody  \n\n")
    assert out == "---\nid: x\n---\n\nBody  \n\n"
    out = join_frontmatter({"id": "x"}, "")
    assert out == "---\nid: x\n---\n\n"


def test_join_empty_meta() -> None:
    assert join_frontmatter({}, "b") == "---\n---\n\nb"
    # split is byte-exact after the closing fence, so the separator blank line comes back.
    assert split_frontmatter(join_frontmatter({}, "b")) == ({}, "\nb")


def test_join_unicode_not_escaped() -> None:
    out = join_frontmatter({"title": "Parseur Mermaid — ünïcode 日本"}, "")
    assert "Parseur Mermaid — ünïcode 日本" in out


def test_round_trip_preserves_key_order_nested() -> None:
    meta = {
        "id": "node-parser",
        "type": "lld",
        "title": "Mermaid parser",
        "status": "todo",
        "owner": None,
        "depends_on": ["node-files", "node-zeta", "node-alpha"],
        "interfaces": [
            "parse(text) -> MermaidDoc",
            {"name": "serialize", "kind": "fn"},
        ],
        "zeta_extra": {"z": 1, "a": {"y": [3, 2, 1], "b": True}},
        "alpha_extra": "kept after interfaces",
    }
    body = "\nFree body.\n\n```mermaid\nflowchart TD\n    a --> b\n```\n   \n"
    text = join_frontmatter(meta, body)
    meta2, body2 = split_frontmatter(text)
    assert meta2 == meta
    assert list(meta2) == list(meta)
    assert list(meta2["zeta_extra"]) == ["z", "a"]
    assert list(meta2["zeta_extra"]["a"]) == ["y", "b"]
    # join strips leading newlines then emits one separator blank line; split
    # hands that blank line back byte-exact.
    assert body2 == "\n" + body.lstrip("\n")
    # A second join of the split result is byte-identical (idempotent).
    assert join_frontmatter(meta2, body2) == text


def test_round_trip_yaml_text_is_block_style() -> None:
    text = join_frontmatter({"depends_on": ["a", "b"], "nested": {"k": "v"}}, "")
    assert "depends_on:\n- a\n- b\n" in text or "depends_on:\n  - a\n  - b\n" in text
    assert "nested:\n  k: v\n" in text


def test_order_keys() -> None:
    meta = {"zzz": 1, "title": "T", "id": "node-a", "status": "todo", "extra": 2, "type": "hld"}
    out = order_keys(meta, NODE_ORDER)
    assert list(out) == ["id", "type", "title", "status", "zzz", "extra"]
    assert out == meta
    assert out is not meta
    # Original untouched.
    assert list(meta) == ["zzz", "title", "id", "status", "extra", "type"]


def test_order_keys_missing_first_keys_skipped() -> None:
    assert list(order_keys({"b": 1, "a": 2}, ["x", "a", "y"])) == ["a", "b"]
    assert order_keys({}, NODE_ORDER) == {}
