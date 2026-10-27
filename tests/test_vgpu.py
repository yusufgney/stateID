"""The GPU proxy and keeper (stateid-vgpu, built with cargo) with a fake vtest server: bytes and fds
pass unchanged, nothing is lost while the proxy is detached for a freeze, a second copy of a session
cannot take over, and the keeper follows and cleans up after each connection."""
import array
import mmap
import os
import shutil
import signal
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
    # its own process group, so that ending it ends the program it runs too
    return subprocess.Popen([exe, "proxy", f"{base}/keeper", "--", "sleep", "600"], start_new_session=True,
                            env=dict(os.environ, XDG_RUNTIME_DIR=run_dir), stderr=subprocess.PIPE)


def end(p):
    try:
        os.killpg(p.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    p.wait()


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
    end(proxy)
    keeper.kill()
    keeper.wait()
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
    assert other.wait(10) == 1  # before it starts its program
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
        end(proxy)  # the session ends without detaching
        assert keeper.wait(5) == 0
        time.sleep(0.2)
        assert not os.path.exists(f"/proc/{children[0]}/cmdline") or \
            Path(f"/proc/{children[0]}/stat").read_text().split(") ")[1][0] == "Z"
        assert not os.path.exists(f"{base}/keeper") and not os.path.exists(f"{base}/vtest")
    finally:
        keeper.kill()
        shutil.rmtree(base)


def vt(cmd, *dwords):
    return struct.pack(f"<II{len(dwords)}I", len(dwords), cmd, *dwords)


@pytest.mark.skipif(not shutil.which("virgl_test_server"), reason="needs virgl_test_server")
def test_move_rebuilds_on_a_new_server_with_the_same_shared_memory_and_contents(exe, request, monkeypatch):
    """Moving to a new server keeps a buffer filled through shared memory (the new server reads the
    program's own memory) and a texture whose contents only the GPU holds (they are read back first):
    reading both back on the new server proves it."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(request.config.cache.mkdir("stateid-vgpu")))
    base = f"/tmp/sgt-{os.getpid()}-{time.monotonic_ns()}"
    os.mkdir(base)
    keeper_sock = vgpu.start_keeper(base, "k")
    proxy = subprocess.Popen([exe, "proxy", keeper_sock, "--", "sleep", "600"], start_new_session=True,
                             env=dict(os.environ, XDG_RUNTIME_DIR=base))
    try:
        wait_for(f"{base}/gpu.ctl", proxy)
        c = socket.socket(socket.AF_UNIX)
        c.connect(f"{base}/gpu.vtest")
        c.settimeout(10)
        c.sendall(struct.pack("<II", 4, 8) + b"test")  # CREATE_RENDERER
        c.sendall(vt(11, 3))  # PROTOCOL_VERSION
        assert recv_exactly(c, 12)[:8] == struct.pack("<II", 1, 11)
        def create(*args):
            c.sendall(vt(RES_CREATE2, 0, *args, 64))  # 64 bytes of shared memory
            res = struct.unpack("<III", recv_exactly(c, 12))[2]
            _, fds = recv_chunk(c)
            shm = mmap.mmap(fds[0], 64)
            os.close(fds[0])
            return res, shm

        def transfer(cmd, res, w, h):  # TRANSFER_PUT2 / GET2 of all of it, then wait until it is done
            c.sendall(vt(cmd, res, 0, 0, 0, 0, w, h, 1, 0, 0) + vt(7, res, 1))
            recv_exactly(c, 12)
        buf, buf_shm = create(0, 64, 0x10, 64, 1, 1, 1, 0, 0)  # a 64-byte buffer
        tex, tex_shm = create(2, 67, 0xa, 4, 4, 1, 1, 0, 0)  # a 4x4 RGBA texture (sampler, render target)
        buf_shm[:5] = b"hello"
        transfer(14, buf, 64, 1)
        tex_shm[:] = bytes(range(64))
        transfer(14, tex, 4, 4)
        tex_shm[:] = bytes(64)  # now only the GPU has it
        assert vgpu.control(base, "move")
        buf_shm[:5] = bytes(5)
        transfer(13, buf, 64, 1)  # read back from the new server into our memory
        assert buf_shm[:5] == b"hello"
        transfer(13, tex, 4, 4)
        assert tex_shm[:] == bytes(range(64))
        log = Path(f"{base}/k.log").read_text()
        assert "connection 1 rebuilt" in log and "contents of 1 read back" in log
    finally:
        end(proxy)
        for _ in range(100):  # the keeper ends with the session and stops its server
            if not os.path.exists(keeper_sock):
                break
            time.sleep(0.05)
        shutil.rmtree(base)


def test_control_without_a_gpu_session(tmp_path):
    assert vgpu.control(tmp_path, "detach") is False


def test_wrapped_command_is_recognised():
    assert vgpu.is_wrapped(["/cache/stateid-vgpu-0123", "proxy", "/k", "--", "app"])
    assert vgpu.is_wrapped(["/venv/python", "-m", "stateid.vgpu", "proxy", "/k", "--", "app"])
    assert not vgpu.is_wrapped(["app", "-m", "x"])


def test_server_gets_the_preload_library(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    monkeypatch.delenv("LD_PRELOAD", raising=False)
    if not (shutil.which("cc") or shutil.which("gcc")):
        assert "LD_PRELOAD" not in vgpu.server_env("nvidia")  # runs without it
        return
    env = vgpu.server_env("nvidia")
    lib = env["LD_PRELOAD"]
    assert lib.startswith(str(tmp_path)) and os.path.exists(lib)
    assert vgpu.server_env("intel")["LD_PRELOAD"] == lib  # built once
