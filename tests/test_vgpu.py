"""The GPU proxy and keeper with a fake vtest server: bytes and fds pass unchanged, nothing is lost
while the proxy is detached for a freeze, and a second copy of a session cannot take over."""
import os
import socket
import threading
import time

import pytest

from stateid import vgpu


def fake_server(path):
    """Echoes what it reads; on b"F" it answers like vtest sends an fd: a reply, then one byte with the fd."""
    srv = socket.socket(socket.AF_UNIX)
    srv.bind(path)
    srv.listen()

    def serve(c):
        while data := c.recv(4096):
            if data == b"F":
                fd = os.memfd_create("res")
                os.write(fd, b"shared")
                c.sendall(b"reply")
                vgpu.send_chunk(c, b"\0", [fd])
            else:
                c.sendall(data)

    def loop():
        while True:
            c, _ = srv.accept()
            threading.Thread(target=serve, args=(c,), daemon=True).start()
    threading.Thread(target=loop, daemon=True).start()


@pytest.fixture
def gpu(tmp_path):
    base = f"/tmp/sgt-{os.getpid()}-{time.monotonic_ns()}"  # unix socket paths must stay short
    os.mkdir(base)
    fake_server(f"{base}/vtest")
    keeper = vgpu.Keeper(f"{base}/keeper", f"{base}/vtest")
    threading.Thread(target=keeper.run, daemon=True).start()
    for _ in range(100):
        if os.path.exists(f"{base}/keeper"):
            break
        time.sleep(0.01)
    proxy = vgpu.Proxy(f"{base}/gpu.vtest", f"{base}/gpu.ctl", f"{base}/keeper")
    proxy.start()
    for _ in range(100):
        if os.path.exists(f"{base}/gpu.ctl"):
            break
        time.sleep(0.01)
    yield base, proxy
    for f in os.listdir(base):
        os.unlink(f"{base}/{f}")
    os.rmdir(base)


def client(base):
    c = socket.socket(socket.AF_UNIX)
    c.connect(f"{base}/gpu.vtest")
    c.settimeout(5)
    return c


def test_bytes_pass_both_ways(gpu):
    c = client(gpu[0])
    c.sendall(b"hello")
    assert c.recv(100) == b"hello"


def test_fd_stays_on_its_own_byte(gpu):
    c = client(gpu[0])
    c.sendall(b"F")
    got = b""
    while len(got) < 5:
        got += c.recv(5 - len(got))  # a plain read must not swallow the fd
    assert got == b"reply"
    data, fds = vgpu.recv_chunk(c)
    assert data == b"\0" and len(fds) == 1
    assert os.pread(fds[0], 6, 0) == b"shared"
    os.close(fds[0])


def test_nothing_is_lost_while_detached(gpu):
    base, proxy = gpu
    c = client(base)
    c.sendall(b"before")
    assert c.recv(100) == b"before"
    assert vgpu.control(base, "detach")
    assert proxy.link is None  # no connection leaves the session now
    c.sendall(b"during")  # waits in the proxy
    time.sleep(0.2)
    assert vgpu.control(base, "attach")
    assert c.recv(100) == b"during"


def test_a_second_copy_is_refused(gpu):
    base, _ = gpu
    other = vgpu.Proxy(f"{base}/o.vtest", f"{base}/o.ctl", f"{base}/keeper")
    with pytest.raises(RuntimeError, match="refused"):
        other.connect()


def test_control_without_a_gpu_session(tmp_path):
    assert vgpu.control(tmp_path, "detach") is False


def test_wrapped_command_is_recognised():
    assert vgpu.is_wrapped(["/venv/python", "-m", "stateid.vgpu", "proxy", "/k", "--", "app"])
    assert not vgpu.is_wrapped(["app", "-m", "x"])
