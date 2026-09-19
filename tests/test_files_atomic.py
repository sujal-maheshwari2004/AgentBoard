import os
from pathlib import Path

import pytest

from whiteboard.files import SelfWriteRegistry, atomic_write, read_bytes


def test_atomic_write_creates_and_replaces(tmp_path: Path) -> None:
    target = tmp_path / "node.md"
    atomic_write(target, b"first")
    assert target.read_bytes() == b"first"
    atomic_write(target, b"second, longer content")
    assert target.read_bytes() == b"second, longer content"
    atomic_write(target, b"x")
    assert target.read_bytes() == b"x"
    assert sorted(os.listdir(tmp_path)) == ["node.md"], "no temp files left behind"


def test_atomic_write_temp_name_is_dotfile_in_same_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []
    real_replace = os.replace

    def spy(src, dst):
        seen.append(os.fspath(src))
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", spy)
    target = tmp_path / "sub" / "card.md"
    target.parent.mkdir()
    atomic_write(target, b"hello")
    assert seen == [str(tmp_path / "sub" / f".card.md.tmp.{os.getpid()}")]
    assert target.read_bytes() == b"hello"


def test_atomic_write_no_fsync(tmp_path: Path) -> None:
    target = tmp_path / "a.md"
    atomic_write(target, b"data", fsync=False)
    assert target.read_bytes() == b"data"
    assert os.listdir(tmp_path) == ["a.md"]


def test_atomic_write_failure_cleans_temp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "a.md"
    target.write_bytes(b"old")

    def boom(src, dst):
        raise OSError("disk on fire")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        atomic_write(target, b"new")
    assert target.read_bytes() == b"old"
    assert os.listdir(tmp_path) == ["a.md"]


def test_atomic_write_file_mode(tmp_path: Path) -> None:
    target = tmp_path / "m.md"
    atomic_write(target, b"z")
    assert oct(target.stat().st_mode & 0o777) in {"0o644", "0o600", "0o640"}  # umask-dependent, never wider than 644


def test_read_bytes_missing_returns_none(tmp_path: Path) -> None:
    assert read_bytes(tmp_path / "nope.md") is None
    assert read_bytes(tmp_path / "nope" / "deeper.md") is None
    p = tmp_path / "yes.md"
    p.write_bytes(b"\x00binary\xff")
    assert read_bytes(p) == b"\x00binary\xff"


def test_registry_echo_once(tmp_path: Path) -> None:
    reg = SelfWriteRegistry()
    p = tmp_path / "n.md"
    reg.note(p, b"abc")
    assert reg.is_echo(p, b"abc") is True
    assert reg.is_echo(p, b"abc") is False, "forgets after one echo"


def test_registry_mismatch_forgets(tmp_path: Path) -> None:
    reg = SelfWriteRegistry()
    p = tmp_path / "n.md"
    reg.note(p, b"abc")
    assert reg.is_echo(p, b"external edit") is False
    assert reg.is_echo(p, b"abc") is False, "mismatch also forgets the entry"


def test_registry_unknown_path(tmp_path: Path) -> None:
    reg = SelfWriteRegistry()
    assert reg.is_echo(tmp_path / "never.md", b"") is False


def test_registry_note_overwrites(tmp_path: Path) -> None:
    reg = SelfWriteRegistry()
    p = tmp_path / "n.md"
    reg.note(p, b"v1")
    reg.note(p, b"v2")
    assert reg.is_echo(p, b"v1") is False
    reg.note(p, b"v2")
    assert reg.is_echo(p, b"v2") is True


def test_registry_keys_by_resolved_path(tmp_path: Path) -> None:
    reg = SelfWriteRegistry()
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real_dir)
    reg.note(link / "n.md", b"same")
    assert (real_dir / "n.md") in reg
    assert reg.is_echo(real_dir / "n.md", b"same") is True
    assert len(reg) == 0
