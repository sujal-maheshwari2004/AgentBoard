import json
import multiprocessing
import os
from pathlib import Path

from whiteboard.files import JsonlTailer, append_jsonl

N_PROCS = 4
N_RECORDS = 200


def _writer(path: str, worker: int) -> None:
    payload = "x" * 1000
    for i in range(N_RECORDS):
        append_jsonl(Path(path), {"worker": worker, "i": i, "payload": payload})


def test_append_creates_file_single_line(tmp_path: Path) -> None:
    p = tmp_path / "events.jsonl"
    append_jsonl(p, {"seq": 1, "note": "héllo — 日本", "data": {"a": [1, 2]}})
    append_jsonl(p, {"seq": 2})
    raw = p.read_text(encoding="utf-8")
    assert raw == '{"seq":1,"note":"héllo — 日本","data":{"a":[1,2]}}\n{"seq":2}\n'
    assert oct(p.stat().st_mode & 0o777) in {"0o644", "0o600", "0o640"}


def test_multi_process_append_no_corruption(tmp_path: Path) -> None:
    p = tmp_path / "events.jsonl"
    ctx = multiprocessing.get_context("spawn")
    procs = [ctx.Process(target=_writer, args=(str(p), w)) for w in range(N_PROCS)]
    for pr in procs:
        pr.start()
    for pr in procs:
        pr.join(timeout=60)
        assert pr.exitcode == 0
    lines = p.read_bytes().split(b"\n")
    assert lines[-1] == b""
    lines = lines[:-1]
    assert len(lines) == N_PROCS * N_RECORDS
    seen: set[tuple[int, int]] = set()
    for line in lines:
        rec = json.loads(line)
        assert len(rec["payload"]) == 1000
        seen.add((rec["worker"], rec["i"]))
    assert len(seen) == N_PROCS * N_RECORDS


def test_tailer_incremental(tmp_path: Path) -> None:
    p = tmp_path / "events.jsonl"
    t = JsonlTailer(p)
    assert t.read_new() == []  # missing file
    append_jsonl(p, {"seq": 1})
    append_jsonl(p, {"seq": 2})
    assert [r["seq"] for r in t.read_new()] == [1, 2]
    assert t.read_new() == []
    append_jsonl(p, {"seq": 3})
    assert [r["seq"] for r in t.read_new()] == [3]
    assert t.read_new() == []


def test_tailer_torn_line(tmp_path: Path) -> None:
    p = tmp_path / "events.jsonl"
    t = JsonlTailer(p)
    full = b'{"seq":1,"note":"complete"}\n'
    half = b'{"seq":2,"note":"to'
    rest = b'rn"}\n'
    with open(p, "ab") as f:
        f.write(full + half)
    assert [r["seq"] for r in t.read_new()] == [1]
    assert t.read_new() == []  # nothing new, partial still buffered
    with open(p, "ab") as f:
        f.write(rest)
    got = t.read_new()
    assert got == [{"seq": 2, "note": "torn"}]
    assert t.read_new() == []


def test_tailer_truncation_restarts(tmp_path: Path) -> None:
    p = tmp_path / "events.jsonl"
    t = JsonlTailer(p)
    for i in range(5):
        append_jsonl(p, {"seq": i})
    assert len(t.read_new()) == 5
    p.write_bytes(b"")  # truncate
    append_jsonl(p, {"seq": 100})
    assert [r["seq"] for r in t.read_new()] == [100]


def test_tailer_rotation_new_inode(tmp_path: Path) -> None:
    p = tmp_path / "events.jsonl"
    t = JsonlTailer(p)
    for i in range(3):
        append_jsonl(p, {"seq": i})
    assert len(t.read_new()) == 3
    os.rename(p, tmp_path / "events.jsonl.1")
    append_jsonl(p, {"seq": 7})
    append_jsonl(p, {"seq": 8})
    # The new file is shorter than the old offset -> reset covers it either way.
    assert [r["seq"] for r in t.read_new()] == [7, 8]


def test_tailer_rotation_same_size_detected_by_inode(tmp_path: Path) -> None:
    p = tmp_path / "events.jsonl"
    t = JsonlTailer(p)
    append_jsonl(p, {"seq": 1})
    assert len(t.read_new()) == 1
    os.rename(p, tmp_path / "old.jsonl")
    append_jsonl(p, {"seq": 2})  # same byte length as the previous file
    assert [r["seq"] for r in t.read_new()] == [2]


def test_tailer_skips_garbage_and_blank_lines(tmp_path: Path) -> None:
    p = tmp_path / "events.jsonl"
    p.write_bytes(b'{"seq":1}\n\nnot json\n[1,2]\n{"seq":2}\n')
    t = JsonlTailer(p)
    assert [r["seq"] for r in t.read_new()] == [1, 2]


def test_tailer_read_all_resets_to_end(tmp_path: Path) -> None:
    p = tmp_path / "events.jsonl"
    t = JsonlTailer(p)
    assert t.read_all() == []
    for i in range(4):
        append_jsonl(p, {"seq": i})
    assert [r["seq"] for r in t.read_new()] == [0, 1, 2, 3]
    assert [r["seq"] for r in t.read_all()] == [0, 1, 2, 3]
    assert t.read_new() == []
    append_jsonl(p, {"seq": 4})
    assert [r["seq"] for r in t.read_new()] == [4]
    assert [r["seq"] for r in t.read_all()] == [0, 1, 2, 3, 4]
