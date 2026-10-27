"""GPU for freezable sessions (experimental): programs render on the real GPU through virgl, while the
GPU state lives outside the session, so the session can still be frozen.

    program --vtest--> proxy (in the session) ==link==> keeper (outside) --vtest--> virgl_test_server --> GPU

The proxy and the keeper are one Rust program, stateid-vgpu (vgpu_rs/; see its main.rs), built with
cargo on first use into the cache. This module builds and starts it, and sends `detach`/`attach` to a
session's proxy around a freeze.

The server gets a small preloaded library (vgpu_preload.c, built with the system C compiler on
first use) that keeps its buffers in GPU memory on NVIDIA and lets the keeper hand it shared memory when it
rebuilds a program's state; without a compiler it runs, only slower.

On detach the keeper hands the proxy each connection's state, with what the GPU held, so the frozen
image carries it. Restoring an image starts a new keeper where the proxy looks for one; the restored
proxy gives it that state and the shared memory it kept, and the keeper rebuilds it on a new server.
"""
import hashlib
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

SERVER = shutil.which("virgl_test_server") or "/usr/bin/virgl_test_server"
RUST_SOURCE = Path(__file__).with_name("vgpu_rs")


class GpuError(RuntimeError):
    pass


LOG_DAYS = 2  # keeper logs are kept this long after their keeper ended


def cache_dir():
    return Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "stateid"


def binary():
    """The stateid-vgpu program, built from vgpu_rs/ once per source version."""
    files = [RUST_SOURCE / "Cargo.toml", *sorted((RUST_SOURCE / "src").glob("*.rs"))]
    digest = hashlib.sha256()
    for f in files:
        digest.update(f.name.encode() + b"\0" + f.read_bytes())
    exe = cache_dir() / f"stateid-vgpu-{digest.hexdigest()[:12]}"
    if exe.exists():
        return exe
    cargo = shutil.which("cargo")
    if not cargo:
        raise GpuError("--gpu needs Rust's cargo to build its relay once (sudo apt install cargo)")
    target = cache_dir() / "vgpu-target"
    r = subprocess.run([cargo, "build", "--release", "--offline", "--quiet", "--manifest-path",
                        str(RUST_SOURCE / "Cargo.toml"), "--target-dir", str(target)],
                       stdin=subprocess.DEVNULL, capture_output=True, text=True)
    if r.returncode:
        raise GpuError("building stateid-vgpu failed:\n" + r.stderr[-2000:])
    tmp = exe.with_suffix(f".{os.getpid()}.tmp")
    shutil.copy2(target / "release" / "stateid-vgpu", tmp)
    tmp.replace(exe)
    return exe


# ---------------------------------------------------------------- control from stateid
def control(run_dir, cmd, timeout=15):
    """Send detach/attach/move to a session's proxy. False if the session has no GPU proxy."""
    return ask(run_dir, cmd, timeout) is not None


def ask(run_dir, cmd, timeout=15):
    """Like control(), but returns the proxy's note for the user ("" for none), None without a proxy."""
    path = Path(run_dir) / "gpu.ctl"
    if not path.exists():
        return None
    try:
        with socket.socket(socket.AF_UNIX) as s:
            s.settimeout(timeout)
            s.connect(str(path))
            s.sendall(cmd.encode() + b"\n")
            answer = s.recv(256).decode(errors="replace").strip()
    except OSError as e:
        raise GpuError(f"GPU proxy: {e}") from e
    if answer != "ok" and not answer.startswith("ok "):
        raise GpuError(f"GPU proxy: {answer or 'no answer'}")
    return answer[3:]


def start_keeper(sock_dir, name, gpu=None):
    """Start virgl_test_server and the keeper for one session, outside it. Returns the keeper socket.
    Both end when the session does."""
    sock_dir = Path(sock_dir)
    sock_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    for log in sock_dir.glob("*.log"):  # of keepers that ended a while ago
        try:
            if not log.with_suffix(".keeper").exists() and time.time() - log.stat().st_mtime > LOG_DAYS * 86400:
                log.unlink()
        except OSError:
            pass
    server_sock, keeper_sock = sock_dir / f"{name}.vtest", sock_dir / f"{name}.keeper"
    keeper_sock.unlink(missing_ok=True)  # a stale one would look ready
    server = ["env", *(f"{k}={v}" for k, v in server_env(gpu).items()),
              SERVER, "--use-egl-surfaceless", "--multi-clients", "--socket-path", str(server_sock)]
    subprocess.Popen([str(binary()), "keeper", str(keeper_sock), str(server_sock), "--", *server],
                     start_new_session=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=open(sock_dir / f"{name}.log", "ab"))
    for _ in range(200):
        if keeper_sock.exists():
            return str(keeper_sock)
        time.sleep(0.05)
    raise GpuError("the GPU keeper did not start (see " + str(sock_dir / f"{name}.log") + ")")


def wrap(argv, gpu=None):
    """The command line that runs `argv` in a freezable session with GPU rendering."""
    name = f"{os.getpid()}-{int(time.time() * 1000)}-{gpu or 'auto'}"  # the GPU, for a restore
    sock_dir = Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp")) / "stateid-gpu"
    keeper = start_keeper(sock_dir, name, gpu)
    return [str(binary()), "proxy", keeper, "--", *argv]


def is_wrapped(argv):
    return (len(argv) > 3 and argv[1:3] == ["-m", "stateid.vgpu"]  # the Python proxy, before stateid-vgpu
            or len(argv) > 3 and Path(argv[0]).name.startswith("stateid-vgpu") and argv[1] == "proxy")


def restart_keeper(argv):
    """Before restoring a session started with --gpu: start a keeper where its proxy looks for one.
    The session's own keeper ended with it (it is waited for); the restored proxy hands the new one
    the GPU state saved in the image, which rebuilds it on a new server."""
    if not (is_wrapped(argv) and Path(argv[0]).name.startswith("stateid-vgpu")):
        raise GpuError("this image was frozen before GPU state was saved with it")
    keeper = Path(argv[2])
    for _ in range(100):
        if not keeper.exists():
            break
        try:
            with socket.socket(socket.AF_UNIX) as s:
                s.connect(str(keeper))
        except OSError:
            keeper.unlink(missing_ok=True)  # left behind
            break
        time.sleep(0.05)
    else:
        raise GpuError(f"the GPU keeper {keeper} is still serving a session")
    name = keeper.name.removesuffix(".keeper")
    gpu = name.rsplit("-", 1)[-1]
    start_keeper(keeper.parent, name, gpu if gpu in ("intel", "nvidia") else None)


def preload_lib():
    """Build vgpu_preload.c once into the cache; None when it cannot be built."""
    src = Path(__file__).with_name("vgpu_preload.c")
    try:
        code = src.read_bytes()
    except OSError:
        return None
    cache = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "stateid"
    lib = cache / f"preload-{hashlib.sha256(code).hexdigest()[:12]}.so"
    if lib.exists():
        return lib
    cc = shutil.which("cc") or shutil.which("gcc")
    if not cc:
        print("stateid: no C compiler, the GPU server runs without vgpu_preload (slower on NVIDIA, cannot be rebuilt)",
              file=sys.stderr)
        return None
    cache.mkdir(parents=True, exist_ok=True)
    tmp = lib.with_suffix(f".{os.getpid()}.tmp")
    r = subprocess.run([cc, "-O2", "-shared", "-fPIC", "-o", str(tmp), str(src), "-ldl"],
                       stdin=subprocess.DEVNULL, capture_output=True, text=True)
    if r.returncode:
        print("stateid: building vgpu_preload failed:\n" + r.stderr, file=sys.stderr)
        tmp.unlink(missing_ok=True)
        return None
    tmp.replace(lib)
    return lib


def server_env(gpu):
    """Environment variables that make virgl_test_server use one GPU: "intel"/"mesa" or "nvidia"."""
    env = {}
    lib = preload_lib()
    if lib:
        env["LD_PRELOAD"] = " ".join(filter(None, [str(lib), os.environ.get("LD_PRELOAD")]))
    vendors = Path("/usr/share/glvnd/egl_vendor.d")
    if gpu == "nvidia":
        env["__EGL_VENDOR_LIBRARY_FILENAMES"] = str(vendors / "10_nvidia.json")
    elif gpu in ("intel", "mesa"):
        env["__EGL_VENDOR_LIBRARY_FILENAMES"] = str(vendors / "50_mesa.json")
    return env
