import os
import socket
import subprocess
import sys
from pathlib import Path

from whiteboard import config


def test_preferred_port_is_deterministic_and_in_range(tmp_path: Path) -> None:
    a = config.preferred_port(tmp_path)
    assert a == config.preferred_port(tmp_path)
    assert config.PORT_BASE <= a < config.PORT_BASE + config.PORT_SPAN
    # symlink-insensitive: resolved path is hashed
    link = tmp_path.parent / (tmp_path.name + "-link")
    link.symlink_to(tmp_path)
    assert config.preferred_port(link) == a


def test_preferred_port_differs_between_projects(tmp_path: Path) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    assert config.preferred_port(tmp_path / "a") != config.preferred_port(tmp_path / "b")


def test_bind_port_prefers_hash_port_then_probes_upward(tmp_path: Path) -> None:
    sock, port = config.bind_port(tmp_path)
    try:
        # A free run should land on the preferred port (or a probe if something else holds it).
        assert config.preferred_port(tmp_path) <= port < config.preferred_port(tmp_path) + config.PORT_PROBES
        # Occupy it and bind again: must move upward, never collide.
        sock.listen(1)
        sock2, port2 = config.bind_port(tmp_path)
        try:
            assert port2 != port
            assert port < port2 < port + config.PORT_PROBES
        finally:
            sock2.close()
    finally:
        sock.close()


def test_bind_port_falls_back_to_os_assigned(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(config, "PORT_PROBES", 0)
    sock, port = config.bind_port(tmp_path)
    try:
        assert port > 0
        assert sock.getsockname()[1] == port
    finally:
        sock.close()


def test_server_json_round_trip(tmp_path: Path) -> None:
    assert config.read_server_json(tmp_path) is None
    info = config.write_server_json(tmp_path, 43111, pid=4242)
    assert config.server_json_path(tmp_path).exists()
    read = config.read_server_json(tmp_path)
    assert read == info
    assert read["pid"] == 4242
    assert read["port"] == 43111
    assert read["project"] == str(tmp_path.resolve())
    assert read["url"] == "http://127.0.0.1:43111"
    assert read["mcp_url"] == "http://127.0.0.1:43111/mcp"
    assert read["version"] == 1
    assert read["started_at"].endswith("Z")
    config.remove_server_json(tmp_path)
    assert config.read_server_json(tmp_path) is None
    config.remove_server_json(tmp_path)  # idempotent


def test_read_server_json_tolerates_garbage(tmp_path: Path) -> None:
    path = config.server_json_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text("{not json")
    assert config.read_server_json(tmp_path) is None
    path.write_text("[1, 2]")
    assert config.read_server_json(tmp_path) is None


def test_pid_alive() -> None:
    assert config.pid_alive(os.getpid())
    assert not config.pid_alive(0)
    assert not config.pid_alive(None)
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    assert not config.pid_alive(proc.pid)


def test_server_healthy_false_when_nothing_listens(tmp_path: Path) -> None:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    info = {"url": f"http://127.0.0.1:{port}", "project": str(tmp_path)}
    assert not config.server_healthy(info, timeout=0.3)
    assert not config.server_healthy(None)
    assert not config.server_healthy({})


def test_is_running_requires_live_pid(tmp_path: Path) -> None:
    config.write_server_json(tmp_path, 1, pid=2_000_000_000)
    healthy, info = config.is_running(tmp_path)
    assert not healthy
    assert info is not None and info["pid"] == 2_000_000_000


def test_agentboard_home_and_names(tmp_path: Path) -> None:
    assert config.AGENTBOARD_HOME.is_dir()
    assert (config.AGENTBOARD_HOME / "whiteboard").is_dir() or os.environ.get("AGENTBOARD_HOME")
    assert config.whiteboard_dir(tmp_path) == tmp_path / ".whiteboard"
    assert config.project_name(tmp_path) == tmp_path.resolve().name
