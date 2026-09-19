import json
import os
import subprocess
from pathlib import Path

import httpx2
import pytest

from whiteboard import liaison
from whiteboard.plan.model import Skeleton


@pytest.fixture
def registry(tmp_path: Path, monkeypatch):
    path = tmp_path / "reg" / "registry.json"
    monkeypatch.setenv("WHITEBOARD_REGISTRY", str(path))
    return path


def dead_pid() -> int:
    proc = subprocess.Popen(["true"])
    proc.wait()
    return proc.pid


# -- registry -----------------------------------------------------------------


def test_registry_path_env_override(registry: Path, monkeypatch):
    assert liaison.registry_path() == registry
    monkeypatch.delenv("WHITEBOARD_REGISTRY")
    assert liaison.registry_path() == Path.home() / ".whiteboard" / "registry.json"


def test_registry_read_missing_and_invalid(registry: Path):
    assert liaison.registry_read() == {}
    registry.parent.mkdir(parents=True)
    registry.write_text("{oops")
    assert liaison.registry_read() == {}
    registry.write_text("[1,2]")
    assert liaison.registry_read() == {}


def test_registry_update_and_prune(registry: Path, tmp_path: Path):
    proj = tmp_path / "proj"
    proj.mkdir()
    out = liaison.registry_update(str(proj), {"port": 43217, "pid": os.getpid(), "started_at": "t"})
    key = str(proj.resolve())
    assert out == {key: {"port": 43217, "pid": os.getpid(), "started_at": "t"}}
    assert json.loads(registry.read_text()) == out
    assert liaison.registry_read() == out

    # a dead entry is pruned on read and on the next update
    data = json.loads(registry.read_text())
    data["/dead/project"] = {"port": 1, "pid": dead_pid(), "started_at": "t"}
    data["/garbage"] = "nope"
    registry.write_text(json.dumps(data))
    assert liaison.registry_read() == out
    out2 = liaison.registry_update("/other", {"port": 2, "pid": os.getpid()})
    assert set(out2) == {key, str(Path("/other").resolve())}

    # removal
    out3 = liaison.registry_update(str(proj), None)
    assert key not in out3 and str(Path("/other").resolve()) in out3
    assert liaison.registry_update("/other", None) == {}
    assert not list(registry.parent.glob("*.tmp"))


def test_pid_alive_semantics(monkeypatch):
    assert liaison.pid_alive(os.getpid())
    assert not liaison.pid_alive(dead_pid())
    assert not liaison.pid_alive(None) and not liaison.pid_alive("x") and not liaison.pid_alive(0)

    def eperm(pid, sig):
        raise PermissionError

    monkeypatch.setattr(os, "kill", eperm)
    assert liaison.pid_alive(12345)


# -- HTTP path (httpx2 MockTransport) ------------------------------------------


def install_peer(monkeypatch, registry: Path, project: Path, handler, port: int = 43999):
    liaison.registry_update(str(project), {"port": port, "pid": os.getpid(), "started_at": "t"})
    real = httpx2.AsyncClient
    seen: dict = {}

    def factory(*args, **kwargs):
        seen["timeout"] = kwargs.get("timeout")
        kwargs["transport"] = httpx2.MockTransport(handler)
        return real(*args, **kwargs)

    monkeypatch.setattr(liaison.httpx2, "AsyncClient", factory)
    return seen


def loader_factory(calls: list, nodes=None):
    def load(path: Path) -> Skeleton:
        calls.append(path)
        return Skeleton(project=str(path), nodes=nodes if nodes is not None else [
            {"id": "node-a", "title": "A", "type": "hld", "status": "todo", "depends_on": []}])
    return load


async def test_fetch_skeleton_http(registry: Path, tmp_path: Path, monkeypatch):
    proj = tmp_path / "peer"
    proj.mkdir()
    requests: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return httpx2.Response(200, json={"project": "peer-live", "nodes": [
            {"id": "node-live", "title": "L", "type": "hld", "status": "done", "depends_on": []}]})

    seen = install_peer(monkeypatch, registry, proj, handler)
    calls: list = []
    sk = await liaison.fetch_skeleton(str(proj), file_loader=loader_factory(calls))
    assert sk.project == "peer-live" and sk.nodes[0]["id"] == "node-live"
    assert calls == []
    assert str(requests[0].url) == "http://127.0.0.1:43999/skeleton"
    assert seen["timeout"] == 2.0


async def test_fetch_skeleton_falls_back_on_http_failure(registry: Path, tmp_path: Path, monkeypatch):
    proj = tmp_path / "peer"
    proj.mkdir()

    def handler(request):
        raise httpx2.ConnectError("refused")

    install_peer(monkeypatch, registry, proj, handler)
    calls: list = []
    sk = await liaison.fetch_skeleton(str(proj), file_loader=loader_factory(calls))
    assert calls == [proj] and sk.project == str(proj) and sk.nodes[0]["id"] == "node-a"


async def test_fetch_skeleton_falls_back_on_bad_status_or_body(registry: Path, tmp_path: Path, monkeypatch):
    proj = tmp_path / "peer"
    proj.mkdir()
    install_peer(monkeypatch, registry, proj, lambda r: httpx2.Response(500, text="boom"))
    calls: list = []
    sk = await liaison.fetch_skeleton(str(proj), file_loader=loader_factory(calls))
    assert calls == [proj]
    install_peer(monkeypatch, registry, proj, lambda r: httpx2.Response(200, json={"nodes": "not-a-list"}))
    sk = await liaison.fetch_skeleton(str(proj), file_loader=loader_factory(calls))
    assert len(calls) == 2 and isinstance(sk, Skeleton)


async def test_fetch_skeleton_without_registry_entry_uses_files(registry: Path, tmp_path: Path, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("no HTTP expected")

    monkeypatch.setattr(liaison.httpx2, "AsyncClient", boom)
    calls: list = []
    sk = await liaison.fetch_skeleton(str(tmp_path / "unregistered"), file_loader=loader_factory(calls))
    assert calls == [tmp_path / "unregistered"] and sk.nodes[0]["id"] == "node-a"


async def test_fetch_skeleton_accepts_async_loader(registry: Path, tmp_path: Path):
    async def load(path: Path) -> Skeleton:
        return Skeleton(project="async", nodes=[])

    sk = await liaison.fetch_skeleton(str(tmp_path), file_loader=load)
    assert sk.project == "async"


async def test_fetch_peer_node_http_and_fallback(registry: Path, tmp_path: Path, monkeypatch):
    proj = tmp_path / "peer"
    proj.mkdir()
    requests: list = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(str(request.url))
        if request.url.path == "/api/nodes/node-live":
            return httpx2.Response(200, json={"id": "node-live", "title": "L", "type": "hld", "status": "todo",
                                              "depends_on": [], "interfaces": [], "body": "hi"})
        return httpx2.Response(404, json={"detail": "not found"})

    install_peer(monkeypatch, registry, proj, handler)
    calls: list = []
    loader = loader_factory(calls, nodes=[
        {"id": "node-a", "title": "A", "type": "hld", "status": "todo", "depends_on": []},
        {"id": "node-b", "title": "B", "type": "lld", "status": "done", "depends_on": ["node-a"]},
    ])
    node = await liaison.fetch_peer_node(str(proj), "node-live", file_loader=loader)
    assert node["id"] == "node-live" and node["body"] == "hi" and calls == []
    assert requests == ["http://127.0.0.1:43999/api/nodes/node-live"]

    node = await liaison.fetch_peer_node(str(proj), "node-b", file_loader=loader)
    assert node == {"id": "node-b", "title": "B", "type": "lld", "status": "done", "depends_on": ["node-a"]}
    assert calls == [proj]

    with pytest.raises(KeyError):
        await liaison.fetch_peer_node(str(proj), "node-nope", file_loader=loader)


async def test_fetch_peer_node_file_only(registry: Path, tmp_path: Path):
    calls: list = []
    node = await liaison.fetch_peer_node(str(tmp_path), "node-a", file_loader=loader_factory(calls))
    assert node["id"] == "node-a" and calls == [tmp_path]
