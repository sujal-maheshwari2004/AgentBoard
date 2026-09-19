"""whiteboard.files — atomic writes, frontmatter, JSONL, watchdog bridge (T1)."""

from whiteboard.files.atomic import SelfWriteRegistry, atomic_write, read_bytes
from whiteboard.files.frontmatter import join_frontmatter, order_keys, split_frontmatter
from whiteboard.files.jsonl import JsonlTailer, append_jsonl
from whiteboard.files.watcher import MarkdownBridge, debounce_worker, start_observer, stop_observer

__all__ = [
    "atomic_write",
    "read_bytes",
    "SelfWriteRegistry",
    "split_frontmatter",
    "join_frontmatter",
    "order_keys",
    "append_jsonl",
    "JsonlTailer",
    "MarkdownBridge",
    "start_observer",
    "debounce_worker",
    "stop_observer",
]
