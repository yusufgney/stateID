"""The GPU proxy and keeper (stateid-vgpu, built with cargo) with a fake vtest server: bytes and fds
pass unchanged, nothing is lost while the proxy is detached for a freeze, a second copy of a session
cannot take over, and the keeper follows and cleans up after each connection."""
import array
import os
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from stateid import vgpu

RES_CREATE2 = 12
pytestmark = pytest.mark.skipif(not shutil.which("cargo"), reason="needs cargo to build stateid-vgpu")


def recv_chunk(sock):
    data, anc, _, _ = sock.recvmsg(1 << 16, socket.CMSG_SPACE(32 * 4))
    fds = []
    for level, kind, payload in anc:
        if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
            fds += array.array("i", payload[: len(payload) - len(payload) % 4])
    return data, fds


def send_fd(sock, fd):
    """As vtest does: one byte carrying the fd."""
    sock.sendmsg([b"\0"], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", [fd]))])
    os.close(fd)


def fake_server(path):
    """Echoes what it reads; on b"F" it answers like vtest sends an fd: a reply, then one byte with
    the fd; a RESOURCE_CREATE2 gets resource 1 and a shared-memory fd."""
    srv = socket.socket(socket.AF_UNIX)
    srv.bind(path)
    srv.listen()

    def serve(c):
        while data := c.recv(4096):
            if data[4:8] == struct.pack("<I", RES_CREATE2):
                c.sendall(struct.pack("<III", 1, RES_CREATE2, 1))
                send_fd(c, os.memfd_create("res"))
            elif data == b"F":
                fd = os.memfd_create("res")
                os.write(fd, b"shared")
                c.sendall(b"reply")
                send_fd(c, fd)
            else:
                c.sendall(data)

    def loop():
        while True:
            c, _ = srv.accept()
            threading.Thread(target=serve, args=(c,), daemon=True).start()
    threading.Thread(target=loop, daemon=True).start()


@pytest.fixture(scope="session")
def exe(tmp_path_factory, request):
    cache = request.config.cache.mkdir("stateid-vgpu")  # kept between runs: cargo builds once
    old = os.environ.get("XDG_CACHE_HOME")
    os.environ["XDG_CACHE_HOME"] = str(cache)
    try:
        return str(vgpu.binary())
    finally:
        if old is None:
            del os.environ["XDG_CACHE_HOME"]
        else:
            os.environ["XDG_CACHE_HOME"] = old


def wait_for(path, proc=None):
    for _ in range(200):
        if os.path.exists(path):
            return
        if proc and proc.poll() is not None:
            break
        time.sleep(0.02)
    raise AssertionError(f"{path} did not appear")


def start_proxy(exe, base, run_dir):
    os.makedirs(run_dir, exist_ok=True)
    p = subprocess.Popen([exe, "proxy", f"{base}/keeper", "--", "sleep", "600"],
                         env=dict(os.environ, XDG_RUNTIME_DIR=run_dir), stderr=subprocess.PIPE)
    return p


@pytest.fixture
def gpu(exe):
    base = f"/tmp/sgt-{os.getpid()}-{time.monotonic_ns()}"  # unix socket paths must stay short
    os.mkdir(base)
    fake_server(f"{base}/vtest")
    keeper = subprocess.Popen([exe, "keeper", f"{base}/keeper", f"{base}/vtest"],
                              stderr=open(f"{base}/keeper.log", "wb"))
    wait_for(f"{base}/keeper", keeper)
    proxy = start_proxy(exe, base, f"{base}/run")
    wait_for(f"{base}/run/gpu.ctl", proxy)
    yield base, proxy, keeper
    for p in (proxy, keeper):
        p.kill()
        p.wait()
    shutil.rmtree(base)


def client(base):
    c = socket.socket(socket.AF_UNIX)
    c.connect(f"{base}/run/gpu.vtest")
    c.settimeout(5)
    return c


def recv_exactly(c, n):
    got = b""
    while len(got) < n:
        got += c.recv(n - len(got))  # a plain read must not swallow an fd
    return got


def test_bytes_pass_both_ways(gpu):
    c = client(gpu[0])
    c.sendall(b"hello")
    assert c.recv(100) == b"hello"


def test_fd_stays_on_its_own_byte(gpu):
    c = client(gpu[0])
    c.sendall(b"F")
    assert recv_exactly(c, 5) == b"reply"
    data, fds = recv_chunk(c)
    assert data == b"\0" and len(fds) == 1
    assert os.pread(fds[0], 6, 0) == b"shared"
    os.close(fds[0])


def test_nothing_is_lost_while_detached(gpu):
    base = gpu[0]
    c = client(base)
    c.sendall(b"before")
    assert c.recv(100) == b"before"
    assert vgpu.control(f"{base}/run", "detach")
    c.sendall(b"during")  # waits in the proxy
    c.settimeout(0.3)
    with pytest.raises(TimeoutError):
        c.recv(100)
    c.settimeout(5)
    assert vgpu.control(f"{base}/run", "attach")
    assert c.recv(100) == b"during"


def test_detached_proxy_keeps_no_link_open(gpu):
    base, proxy, _ = gpu
    assert vgpu.control(f"{base}/run", "detach")
    links = [os.readlink(f"/proc/{proxy.pid}/fd/{fd}") for fd in os.listdir(f"/proc/{proxy.pid}/fd")]
    sockets = [x for x in links if x.startswith("socket:")]
    assert len(sockets) == 2  # the program and control sockets it listens on, nothing else
    assert vgpu.control(f"{base}/run", "attach")


def test_a_second_copy_is_refused(gpu, exe):
    base = gpu[0]
    other = start_proxy(exe, base, f"{base}/other")
    assert other.wait(10) == 1
    assert b"refused" in other.stderr.read()


def test_keeper_follows_what_each_connection_sets_up(gpu):
    base = gpu[0]
    c = client(base)
    c.sendall(struct.pack("<13I", 11, RES_CREATE2, 0, 2, 1, 0, 64, 64, 1, 1, 0, 0, 4096))
    assert recv_exactly(c, 12) == struct.pack("<III", 1, RES_CREATE2, 1)
    _, fds = recv_chunk(c)
    assert len(fds) == 1
    os.close(fds[0])
    c.close()
    for _ in range(100):
        log = Path(f"{base}/keeper.log").read_text()
        if "closed" in log:
            break
        time.sleep(0.02)
    assert "connection 1 closed" in log and "1 resources; could rebuild: yes" in log


def test_keeper_ends_with_its_session_and_stops_the_server(exe):
    base = f"/tmp/sgt-{os.getpid()}-{time.monotonic_ns()}"
    os.mkdir(base)
    server = [sys.executable, "-c", "import socket, sys, time; s = socket.socket(socket.AF_UNIX); "
              "s.bind(sys.argv[1]); s.listen(); time.sleep(600)", f"{base}/vtest"]
    keeper = subprocess.Popen([exe, "keeper", f"{base}/keeper", f"{base}/vtest", "--", *server])
    try:
        wait_for(f"{base}/keeper", keeper)
        children = Path(f"/proc/{keeper.pid}/task/{keeper.pid}/children").read_text().split()
        assert len(children) == 1
        proxy = start_proxy(exe, base, f"{base}/run")
        wait_for(f"{base}/run/gpu.ctl", proxy)
        proxy.kill()  # the session ends without detaching
        proxy.wait()
        assert keeper.wait(5) == 0
        time.sleep(0.2)
        assert not os.path.exists(f"/proc/{children[0]}/cmdline") or \
            Path(f"/proc/{children[0]}/stat").read_text().split(") ")[1][0] == "Z"
        assert not os.path.exists(f"{base}/keeper") and not os.path.exists(f"{base}/vtest")
    finally:
        keeper.kill()
        shutil.rmtree(base)


def test_control_without_a_gpu_session(tmp_path):
    assert vgpu.control(tmp_path, "detach") is False


def test_wrapped_command_is_recognised():
    assert vgpu.is_wrapped(["/cache/stateid-vgpu-0123", "proxy", "/k", "--", "app"])
    assert vgpu.is_wrapped(["/venv/python", "-m", "stateid.vgpu", "proxy", "/k", "--", "app"])
    assert not vgpu.is_wrapped(["app", "-m", "x"])


def test_server_gets_the_staticbuf_library(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    monkeypatch.delenv("LD_PRELOAD", raising=False)
    if not (shutil.which("cc") or shutil.which("gcc")):
        assert "LD_PRELOAD" not in vgpu.server_env("nvidia")  # runs without it
        return
    env = vgpu.server_env("nvidia")
    lib = env["LD_PRELOAD"]
    assert lib.startswith(str(tmp_path)) and os.path.exists(lib)
    assert vgpu.server_env("intel")["LD_PRELOAD"] == lib  # built once
