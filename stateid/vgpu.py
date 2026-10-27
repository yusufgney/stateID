"""GPU for freezable sessions (experimental): programs render on the real GPU through virgl, while the
GPU state lives outside the session, so the session can still be frozen.

    program --vtest--> proxy (in the session) ==link==> keeper (outside) --vtest--> virgl_test_server --> GPU

The program talks to the proxy as if it were virgl_test_server (Mesa's virpipe driver). The proxy keeps
one link to the keeper and multiplexes every program connection over it. CRIU cannot freeze a socket
whose other end is outside the session, so before a freeze the proxy drains and closes the link
(`detach`), and opens it again afterwards (`attach`); meanwhile the keeper holds the server
connections, so the GPU state survives, and buffers what the server sends.

Bytes and SCM_RIGHTS file descriptors are relayed unchanged; a descriptor stays on the one byte it
was sent with (see relay_chunk). Nothing else is parsed, so any vtest client works.

Not done yet: restoring a frozen image later brings the program back without its GPU state (the
keeper's state belongs to the session that kept running), so its rendering breaks.
"""
import array
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

HEADER = struct.Struct("<BIIB")  # type, connection, payload length, number of fds
DATA, OPEN, CLOSE, DETACH, DETACH_ACK, ATTACH, ATTACHED, REFUSED = range(1, 9)
FD_SPACE = socket.CMSG_SPACE(32 * 4)
MESA_ENV = {"LIBGL_ALWAYS_SOFTWARE": "1", "GALLIUM_DRIVER": "virpipe"}
SERVER = shutil.which("virgl_test_server") or "/usr/bin/virgl_test_server"


class GpuError(RuntimeError):
    pass


# ---------------------------------------------------------------- socket helpers
def recv_chunk(sock, size=1 << 16):
    """(bytes, [fds]); b"" and [] at end of stream."""
    data, anc, _, _ = sock.recvmsg(size, FD_SPACE)
    fds = []
    for level, kind, payload in anc:
        if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
            fds += array.array("i", payload[: len(payload) - len(payload) % 4])
    return data, fds


def send_chunk(sock, data, fds=()):
    """Send data with fds attached to its first byte, then close our copies of the fds."""
    try:
        anc = [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", fds))] if fds else []
        sent = sock.sendmsg([data], anc) if data or anc else 0
        if sent < len(data):
            sock.sendall(data[sent:])
    finally:
        for fd in fds:
            os.close(fd)


def relay_chunk(sock, data, fds=()):
    """Pass on a chunk read from a vtest peer. A read returns the bytes before a message carrying fds
    together with it, and the fds belong to that last message: vtest sends each fd with one byte of
    its own, which the other side reads with recvmsg. So the fds go with the last byte."""
    if fds and len(data) > 1:
        send_chunk(sock, data[:-1])
        data = data[-1:]
    send_chunk(sock, data, fds)


class Link:
    """Framed messages over one stream socket; fds travel with the frame they belong to."""

    def __init__(self, sock):
        self.sock, self.buf, self.fds = sock, b"", []
        self.lock = threading.Lock()

    def send(self, kind, conn=0, data=b"", fds=()):
        with self.lock:
            send_chunk(self.sock, HEADER.pack(kind, conn, len(data), len(fds)) + data, list(fds))

    def recv(self):
        """The next frame as (kind, conn, data, fds), or None at end of stream."""
        while True:
            if len(self.buf) >= HEADER.size:
                kind, conn, n, nfds = HEADER.unpack_from(self.buf)
                if len(self.buf) >= HEADER.size + n and len(self.fds) >= nfds:
                    data = self.buf[HEADER.size:HEADER.size + n]
                    self.buf = self.buf[HEADER.size + n:]
                    fds, self.fds = self.fds[:nfds], self.fds[nfds:]
                    return kind, conn, data, fds
            data, fds = recv_chunk(self.sock)
            if not data and not fds:
                return None
            self.buf += data
            self.fds += fds


# ---------------------------------------------------------------- keeper (outside the session)
class Keeper:
    """Holds the connections to virgl_test_server for one session's proxy."""

    MAX_DETACHED = 600  # seconds without a proxy before giving up (a freeze takes seconds)

    def __init__(self, path, server_sock):
        self.path, self.server_sock = path, server_sock
        self.upstreams = {}       # connection -> socket to the server
        self.link = None
        self.pending = []         # frames for the proxy while it is detached
        self.lock = threading.Lock()
        self.ended = False        # the proxy left without detaching: the session is over
        self.detached_since = time.time()

    def to_proxy(self, kind, conn, data=b"", fds=()):
        # one lock for every frame to the proxy: nothing can slip in after DETACH_ACK
        with self.lock:
            if self.link is not None:
                try:
                    self.link.send(kind, conn, data, fds)
                    return
                except OSError:  # the proxy went away: keep it for the next attach
                    self.link, fds = None, []
            self.pending.append((kind, conn, data, list(fds)))

    def pump(self, conn, sock):
        while True:
            try:
                data, fds = recv_chunk(sock)
            except OSError:
                data, fds = b"", []
            if not data and not fds:
                self.to_proxy(CLOSE, conn)
                return
            self.to_proxy(DATA, conn, data, fds)

    def serve_link(self, link):
        while (frame := link.recv()) is not None:
            kind, conn, data, fds = frame
            if kind == OPEN:
                s = socket.socket(socket.AF_UNIX)
                s.connect(self.server_sock)
                self.upstreams[conn] = s
                threading.Thread(target=self.pump, args=(conn, s), daemon=True).start()
            elif kind == DATA and conn in self.upstreams:
                relay_chunk(self.upstreams[conn], data, fds)
            elif kind == CLOSE and conn in self.upstreams:
                self.upstreams.pop(conn).shutdown(socket.SHUT_WR)
            elif kind == DETACH:
                with self.lock:  # nothing more goes to the proxy until it attaches again
                    self.link = None
                    self.detached_since = time.time()
                    link.send(DETACH_ACK)
                return
            elif kind == ATTACH:
                with self.lock:
                    if self.link is not None:  # e.g. a restored copy while the original still runs
                        link.send(REFUSED)
                        return
                    pending, self.pending = self.pending, []
                    for frame in pending:
                        link.send(*frame)
                    self.link = link
                    link.send(ATTACHED)
        with self.lock:
            if self.link is link:  # the proxy (the session) ended
                self.link, self.ended = None, True

    def run(self):
        if os.path.exists(self.path):
            os.unlink(self.path)
        srv = socket.socket(socket.AF_UNIX)
        srv.bind(self.path)
        os.chmod(self.path, 0o600)
        srv.listen()
        srv.settimeout(5)
        while not self.ended:
            if self.link is None and time.time() - self.detached_since > self.MAX_DETACHED:
                return
            try:
                c, _ = srv.accept()
            except TimeoutError:
                continue
            c.settimeout(None)
            threading.Thread(target=self.serve_link, args=(Link(c),), daemon=True).start()


# ---------------------------------------------------------------- proxy (inside the session)
class Proxy:
    def __init__(self, listen, control, keeper):
        self.listen, self.control, self.keeper = listen, control, keeper
        self.clients = {}
        self.next_conn = 1
        self.link = None
        self.attached = threading.Event()
        self.detached = threading.Event()
        self.state = threading.Lock()  # held while detaching/attaching
        self.gate = threading.Lock()   # held while a frame goes out; DETACH takes it last

    def connect(self):
        s = socket.socket(socket.AF_UNIX)
        s.connect(self.keeper)
        link = Link(s)
        link.send(ATTACH)
        while (frame := link.recv()) is not None and frame[0] not in (ATTACHED, REFUSED):
            self.deliver(*frame)
        if frame is None or frame[0] == REFUSED:
            s.close()
            raise RuntimeError("the GPU keeper refused this session (its GPU state belongs to another copy)")
        self.link = link
        threading.Thread(target=self.read_link, args=(link,), daemon=True).start()
        self.attached.set()

    def deliver(self, kind, conn, data, fds):
        c = self.clients.get(conn)
        if c is None:
            for fd in fds:
                os.close(fd)
        elif kind == DATA:
            relay_chunk(c, data, fds)
        elif kind == CLOSE:
            c.shutdown(socket.SHUT_WR)

    def read_link(self, link):
        while (frame := link.recv()) is not None:
            if frame[0] == DETACH_ACK:
                self.detached.set()
                return
            self.deliver(*frame)

    def send(self, kind, conn, data=b"", fds=()):
        while True:
            self.attached.wait()
            with self.gate:
                if self.attached.is_set():
                    self.link.send(kind, conn, data, fds)
                    return

    def client(self, conn, sock):
        self.send(OPEN, conn)
        while True:
            data, fds = recv_chunk(sock)
            if not data and not fds:
                self.send(CLOSE, conn)
                self.clients.pop(conn, None)
                return
            self.send(DATA, conn, data, fds)

    def detach(self):
        with self.state:
            if not self.attached.is_set():
                return
            self.attached.clear()
            self.detached.clear()
            with self.gate:  # waits for a frame being sent; later ones wait for attach
                self.link.send(DETACH)
            if not self.detached.wait(10):
                raise TimeoutError("the GPU keeper did not answer")
            self.link.sock.close()
            self.link = None

    def attach(self):
        with self.state:
            if not self.attached.is_set():
                self.connect()

    def serve_control(self):
        """`detach` / `attach`, one per connection, answered with `ok` or an error line."""
        if os.path.exists(self.control):
            os.unlink(self.control)
        srv = socket.socket(socket.AF_UNIX)
        srv.bind(self.control)
        os.chmod(self.control, 0o600)
        srv.listen()
        while True:
            c, _ = srv.accept()
            with c:
                cmd = c.recv(64).decode(errors="replace").strip()
                try:
                    {"detach": self.detach, "attach": self.attach}[cmd]()
                    c.sendall(b"ok\n")
                except Exception as e:
                    c.sendall(f"error: {e}\n".encode())

    def start(self):
        """Attach to the keeper and serve the program's connections and the control socket."""
        self.connect()
        if os.path.exists(self.listen):
            os.unlink(self.listen)
        srv = socket.socket(socket.AF_UNIX)
        srv.bind(self.listen)
        os.chmod(self.listen, 0o600)
        srv.listen()
        threading.Thread(target=self.serve_control, daemon=True).start()

        def accept():
            while True:
                c, _ = srv.accept()
                conn, self.next_conn = self.next_conn, self.next_conn + 1
                self.clients[conn] = c
                threading.Thread(target=self.client, args=(conn, c), daemon=True).start()
        threading.Thread(target=accept, daemon=True).start()

    def run(self, argv):
        self.start()
        env = dict(os.environ, VTEST_SOCKET_NAME=self.listen, **MESA_ENV)
        return subprocess.call(argv, env=env)


# ---------------------------------------------------------------- control from stateid
def control(run_dir, cmd, timeout=15):
    """Send detach/attach to a session's proxy. False if the session has no GPU proxy."""
    path = Path(run_dir) / "gpu.ctl"
    if not path.exists():
        return False
    try:
        with socket.socket(socket.AF_UNIX) as s:
            s.settimeout(timeout)
            s.connect(str(path))
            s.sendall(cmd.encode() + b"\n")
            answer = s.recv(256).decode(errors="replace").strip()
    except OSError as e:
        raise GpuError(f"GPU proxy: {e}") from e
    if answer != "ok":
        raise GpuError(f"GPU proxy: {answer or 'no answer'}")
    return True


def start_keeper(sock_dir, name, gpu=None):
    """Start virgl_test_server and the keeper for one session, outside it. Returns the keeper socket.
    Both end when the session does."""
    sock_dir = Path(sock_dir)
    sock_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    server_sock, keeper_sock = sock_dir / f"{name}.vtest", sock_dir / f"{name}.keeper"
    keeper_sock.unlink(missing_ok=True)  # a stale one would look ready
    subprocess.Popen([sys.executable, "-m", "stateid.vgpu", "keeper", str(keeper_sock), str(server_sock),
                      gpu or ""], start_new_session=True, stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=open(sock_dir / f"{name}.log", "ab"))
    for _ in range(100):
        if keeper_sock.exists():
            return str(keeper_sock)
        time.sleep(0.05)
    raise GpuError("the GPU keeper did not start (see " + str(sock_dir / f"{name}.log") + ")")


def wrap(argv, gpu=None):
    """The command line that runs `argv` in a freezable session with GPU rendering."""
    name = f"{os.getpid()}-{int(time.time() * 1000)}"
    sock_dir = Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp")) / "stateid-gpu"
    keeper = start_keeper(sock_dir, name, gpu)
    return [sys.executable, "-m", "stateid.vgpu", "proxy", keeper, "--", *argv]


def is_wrapped(argv):
    return len(argv) > 3 and argv[1:3] == ["-m", "stateid.vgpu"]


def server_env(gpu):
    """Environment that makes virgl_test_server use one GPU: "intel"/"mesa" or "nvidia"."""
    env = dict(os.environ)
    vendors = Path("/usr/share/glvnd/egl_vendor.d")
    if gpu == "nvidia":
        env["__EGL_VENDOR_LIBRARY_FILENAMES"] = str(vendors / "10_nvidia.json")
    elif gpu in ("intel", "mesa"):
        env["__EGL_VENDOR_LIBRARY_FILENAMES"] = str(vendors / "50_mesa.json")
    return env


def main():
    mode, args = sys.argv[1], sys.argv[2:]
    if mode == "keeper":  # keeper KEEPER_SOCK SERVER_SOCK [GPU]
        keeper_sock, server_sock = args[:2]
        gpu = args[2] if len(args) > 2 else ""
        if os.path.exists(server_sock):
            os.unlink(server_sock)
        os.umask(0o077)  # the server socket gives full access to the GPU state: this user only
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))  # so the server is stopped too
        server = subprocess.Popen([SERVER, "--use-egl-surfaceless", "--multi-clients", "--socket-path", server_sock],
                                  env=server_env(gpu), stdin=subprocess.DEVNULL)
        for _ in range(100):
            if os.path.exists(server_sock):
                break
            time.sleep(0.05)
        try:
            Keeper(keeper_sock, server_sock).run()
        finally:
            server.send_signal(signal.SIGTERM)
            for p in (keeper_sock, server_sock):
                Path(p).unlink(missing_ok=True)
    elif mode == "proxy":  # proxy KEEPER_SOCK -- PROGRAM [ARGS...]
        keeper_sock, argv = args[0], args[args.index("--") + 1:]
        run_dir = os.environ.get("XDG_RUNTIME_DIR") or "/tmp"
        sys.exit(Proxy(f"{run_dir}/gpu.vtest", f"{run_dir}/gpu.ctl", keeper_sock).run(argv))
    else:
        sys.exit("usage: python -m stateid.vgpu keeper|proxy ...")


if __name__ == "__main__":
    main()
