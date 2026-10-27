"""Client side of freezable sessions: talks to stateid-helperd and shows sessions on the desktop.

A freezable session is a program started with `stateid run`: it runs on its own virtual display
(Xvnc) inside a PID namespace, so the helper can checkpoint it with CRIU, unsaved work included.
On the desktop it appears in a VNC viewer window, which is disconnected while the session is
being frozen (a connection leaving the frozen process tree cannot be checkpointed).
"""
import json
import os
import shutil
import signal
import socket
import subprocess
import time
from pathlib import Path

SOCKET_PATH = os.environ.get("STATEID_HELPER_SOCKET", "/run/stateid/helper.sock")
VIEWERS = ("xtigervncviewer", "vncviewer")


class HelperUnavailable(Exception):
    pass


class HelperError(Exception):
    pass


def available():
    return Path(SOCKET_PATH).exists()


def call(op, timeout=180, **args):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(SOCKET_PATH)
    except OSError as e:
        raise HelperUnavailable(
            "The freeze helper is not running. Install it with `~/stateid/install.sh --with-freeze` "
            f"({e.strerror})") from e
    with s:
        s.sendall(json.dumps({"op": op, **args}).encode() + b"\n")
        data = b""
        while not data.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk:
                break
            data += chunk
    reply = json.loads(data or b"{}")
    if not reply.get("ok"):
        raise HelperError(reply.get("error", "no reply from the helper"))
    return reply["result"]


def start(argv, cwd=None, geometry="1600x900"):
    env = {k: v for k, v in os.environ.items() if len(v) <= 16384}
    return call("start", argv=argv, cwd=cwd or os.getcwd(), env=env, geometry=geometry)


def sessions():
    return call("list")


# ---------------------------------------------------------------- the viewer on the desktop
VIEWER_DIR = Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp")) / "stateid-viewers"


def viewer_command(session):
    exe = next((shutil.which(v) for v in VIEWERS if shutil.which(v)), None)
    if not exe:
        raise HelperUnavailable("No VNC viewer found; install tigervnc-viewer")
    # the window title comes from the session's desktop name: "<program> (stateid <sid>) - TigerVNC"
    return [exe, "-RemoteResize=1", "-ReconnectOnError=0", "-AlertOnFatalError=0", "-Shared=1",
            session["vnc_socket"]]


def show(session):
    """Open (or reopen) the viewer window for a session; remembers its pid."""
    VIEWER_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    p = subprocess.Popen(viewer_command(session), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    (VIEWER_DIR / session["sid"]).write_text(str(p.pid))
    return p.pid


def viewer_pid(sid):
    try:
        pid = int((VIEWER_DIR / sid).read_text())
    except (OSError, ValueError):
        return None
    try:
        cmd = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return None
    return pid if sid.encode() in cmd else None


def _running(pid):
    """False once the process has exited; a viewer started by this process is reaped here,
    otherwise it would stay a zombie and look alive."""
    try:
        if os.waitpid(pid, os.WNOHANG)[0] == pid:
            return False
    except ChildProcessError:
        pass  # not our child
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] != "Z"
    except (OSError, IndexError):
        return False


def hide(sid):
    pid = viewer_pid(sid)
    if pid:
        os.kill(pid, signal.SIGTERM)
        for _ in range(40):
            if not _running(pid):
                break
            time.sleep(0.05)
    (VIEWER_DIR / sid).unlink(missing_ok=True)
    return pid is not None


def freeze(sid):
    """Checkpoint a running session (it keeps running). The viewer reconnects afterwards, and so
    does the GPU proxy of a session started with --gpu (its link leaves the session)."""
    from . import vgpu
    session = next((s for s in sessions()["sessions"] if s["sid"] == sid), None)
    shown = hide(sid)
    try:
        gpu = bool(session) and vgpu.control(session["run_dir"], "detach")
        try:
            return call("freeze", sid=sid, leave_running=True)
        finally:
            if gpu:
                vgpu.control(session["run_dir"], "attach")
    except vgpu.GpuError as e:
        raise HelperError(str(e)) from e
    finally:
        if shown:
            s = next((s for s in sessions()["sessions"] if s["sid"] == sid), None)
            if s:
                show(s)


def restore(iid):
    """Bring a frozen image back. A session still running from the same image's display is
    stopped first; it is replaced by the frozen moment."""
    imgs = {m["iid"]: m for m in sessions()["images"]}
    if iid not in imgs:
        raise HelperError(f"no frozen image {iid}")
    from . import vgpu
    if vgpu.is_wrapped(imgs[iid]["argv"]):
        raise HelperError("restoring a session started with --gpu is not supported yet: its GPU state "
                          "is not saved with the image")
    for s in sessions()["sessions"]:
        if s["display"] == imgs[iid]["display"]:
            hide(s["sid"])
            call("stop", sid=s["sid"])
    s = call("restore", iid=iid)
    show(s)
    return s


def stop(sid):
    hide(sid)
    return call("stop", sid=sid)
