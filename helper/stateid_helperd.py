#!/usr/bin/python3 -I
"""stateid-helperd: freeze and restore stateid sessions with CRIU.

Runs as root (systemd service) because CRIU needs CAP_CHECKPOINT_RESTORE and, with
kernel.yama.ptrace_scope=1, CAP_SYS_PTRACE. To keep that power narrow it only ever acts on
sessions it started itself, for the user who asked:

- the caller is identified by the kernel (SO_PEERCRED), never by what the request says;
- programs run as the caller, never as root, in a process tree of their own;
- before every freeze, kill or restore, every process of the session must belong to the
  caller and hold no capabilities, and the session's first process must be the one recorded
  (PID and start time), so a reused PID cannot be smuggled in;
- checkpoint images live in a root-only directory (/var/lib/stateid/<uid>/images/<id>/) that
  users cannot write: a tampered image restored by root could run anything as root.
  Before a restore the image's credentials are checked against the caller as well;
- there is no "run this as root" operation.

Standard library only (plus pycriu, shipped with criu, for the image check), so the
root-owned copy installed in /usr/local/libexec/stateid/ depends on nothing in the user's
home. Protocol: one JSON request per connection on /run/stateid/helper.sock, one JSON reply.

The same file is also the first process of every session (`--session-init`, run as the user):
it starts the virtual display (Xvnc) and the program, adopts orphans and exits with the program.
"""
import json
import os
import pwd
import re
import secrets
import shutil
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

SOCKET_PATH = "/run/stateid/helper.sock"
STATE_BASE = Path("/var/lib/stateid")
RUN_BASE = Path("/run/stateid")
CRIU = "/usr/sbin/criu"
XVNC = next((p for p in ("/usr/bin/Xtigervnc", "/usr/bin/Xvnc") if os.path.exists(p)), "/usr/bin/Xvnc")
DBUS_RUN_SESSION = "/usr/bin/dbus-run-session"
WM = "/usr/bin/matchbox-window-manager"  # optional: fills the viewer, keeps dialogs on top
PYTHON = "/usr/bin/python3"

MIN_UID = 1000              # system accounts and root cannot use the helper
MAX_SESSIONS = 10           # per user
MAX_IMAGES = 30             # per user
MAX_IMAGE_BYTES = 16 << 30  # per user, all images together
DISPLAYS = range(100, 200)
MAX_REQUEST = 256 << 10

ID_RE = re.compile(r"^[a-z0-9]{12}$")
ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
GEOMETRY_RE = re.compile(r"^[0-9]{3,4}x[0-9]{3,4}$")

# Variables that would connect the program to things outside the frozen tree, or that the
# session sets itself. Everything else the caller sends is passed to the (unprivileged) program.
ENV_DROP = {"DISPLAY", "WAYLAND_DISPLAY", "WAYLAND_SOCKET", "DBUS_SESSION_BUS_ADDRESS",
            "XDG_RUNTIME_DIR", "AT_SPI_BUS_ADDRESS", "XAUTHORITY", "SESSION_MANAGER",
            "PULSE_SERVER", "PIPEWIRE_REMOTE", "SSH_AUTH_SOCK", "GPG_AGENT_INFO", "XDG_SESSION_ID",
            "HOME", "USER", "LOGNAME", "SHELL"}
# What a program needs so that nothing in it holds a device or socket CRIU cannot freeze
# (found in the M0 feasibility test: /dev/udmabuf, portals, gvfs FUSE mounts, the a11y bus).
SESSION_ENV = {
    "GDK_BACKEND": "x11", "QT_QPA_PLATFORM": "xcb", "SDL_VIDEODRIVER": "x11",
    "GSK_RENDERER": "cairo", "GDK_DISABLE": "dmabuf,gl,vulkan", "LIBGL_ALWAYS_SOFTWARE": "1",
    "QT_XCB_GL_INTEGRATION": "none", "GTK_USE_PORTAL": "0", "GIO_USE_VFS": "local",
    "GSETTINGS_BACKEND": "memory", "NO_AT_BRIDGE": "1", "GTK_A11Y": "none",
    "QT_LINUX_ACCESSIBILITY_ALWAYS_ON": "0", "ELECTRON_DISABLE_GPU": "1",
}
DBUS_CONFIG = """<!DOCTYPE busconfig PUBLIC "-//freedesktop//DTD D-Bus Bus Configuration 1.0//EN"
 "http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">
<busconfig>
  <type>session</type>
  <listen>unix:dir={dir}</listen>
  <auth>EXTERNAL</auth>
  <policy context="default">
    <allow send_destination="*" eavesdrop="true"/>
    <allow eavesdrop="true"/>
    <allow own="*"/>
  </policy>
</busconfig>
"""


class Refused(Exception):
    """A request the helper will not carry out; the message goes back to the caller."""


# ---------------------------------------------------------------- /proc
def proc_status(pid):
    """{field: value} from /proc/<pid>/status, or None if the process is gone."""
    try:
        text = Path(f"/proc/{pid}/status").read_text()
    except OSError:
        return None
    return dict(line.split(":\t", 1) for line in text.splitlines() if ":\t" in line)


def start_time(pid):
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        return int(stat.rsplit(")", 1)[1].split()[19])
    except (OSError, ValueError, IndexError):
        return None


def children(pid):
    out = []
    try:
        tasks = os.listdir(f"/proc/{pid}/task")
    except OSError:
        return out
    for tid in tasks:
        try:
            out += [int(c) for c in Path(f"/proc/{pid}/task/{tid}/children").read_text().split()]
        except (OSError, ValueError):
            continue
    return out


def tree(pid):
    """pid and all its descendants."""
    seen, todo = [], [pid]
    while todo:
        p = todo.pop()
        if p not in seen:
            seen.append(p)
            todo += children(p)
    return seen


def check_owned(pids, uid, status=proc_status):
    """Every process must run as `uid` (real, effective, saved, fs) for user and group and hold no
    capabilities; otherwise freezing it would hand someone else's memory to the caller."""
    gid = pwd.getpwuid(uid).pw_gid
    for p in pids:
        st = status(p)
        if st is None:
            continue  # exited meanwhile
        if any(int(u) != uid for u in st.get("Uid", "").split()):
            raise Refused(f"process {p} of the session does not belong to you")
        if any(int(g) != gid for g in st.get("Gid", "").split()):
            raise Refused(f"process {p} of the session runs with another group")
        if int(st.get("CapEff", "0"), 16) or int(st.get("CapPrm", "0"), 16):
            name = (st.get("Name") or "?").strip()
            raise Refused(f"process {p} ({name}) of the session holds capabilities, so it cannot be "
                          "frozen; stop that part of the program and try again")


def check_sandboxes(pids, status=proc_status):
    """CRIU cannot freeze a process in a nested PID namespace. Programs start those briefly, e.g.
    GTK's image loader (glycin) runs in bwrap and lingers about 30 s after a file dialog."""
    for p in pids:
        st = status(p)
        if st and len(st.get("NSpid", "").split()) > 1:
            name = (st.get("Name") or "?").strip()
            raise Refused(f"process {p} ({name}) runs in its own sandbox, which cannot be frozen "
                          "(often an image loader after a file dialog); wait about 30 seconds and try again")


def killable(pids, uid, status=proc_status):
    """The processes the user could kill themselves: every user id is theirs (capabilities are
    fine, unlike for freezing). Others are left alone."""
    out = []
    for p in pids:
        st = status(p)
        if st is not None and all(int(u) == uid for u in st.get("Uid", "").split()):
            out.append(p)
    return out


def session_processes(sid, uid, status=proc_status):
    """Processes still left from session `sid`: every process of the session gets STATEID_SESSION=<sid>
    in its environment, including ones that left the session's process tree. Only the user's own
    processes (see killable) are returned."""
    tag = f"STATEID_SESSION={sid}".encode()
    out = []
    for name in os.listdir("/proc"):
        if not name.isdigit() or int(name) == os.getpid():
            continue
        try:
            env = Path(f"/proc/{name}/environ").read_bytes().split(b"\0")
        except OSError:
            continue
        if tag in env:
            out.append(int(name))
    return killable(out, uid, status)


def end_session_processes(sid, uid, grace=2.0):
    """SIGTERM what is left of a session, then SIGKILL whatever is still there after `grace` seconds."""
    for sig, wait in ((signal.SIGTERM, grace), (signal.SIGKILL, 0)):
        pids = session_processes(sid, uid)
        for p in pids:
            try:
                os.kill(p, sig)
            except ProcessLookupError:
                pass
        deadline = time.time() + wait
        while pids and time.time() < deadline:
            time.sleep(0.05)
            pids = [p for p in pids if proc_status(p) is not None]
        if not pids:
            return


# ---------------------------------------------------------------- request validation
def validate_start(req):
    argv = req.get("argv")
    if not (isinstance(argv, list) and 0 < len(argv) <= 64
            and all(isinstance(a, str) and 0 < len(a) <= 4096 and "\0" not in a for a in argv)):
        raise Refused("argv must be a non-empty list of strings")
    cwd = req.get("cwd") or ""
    if not isinstance(cwd, str) or "\0" in cwd or (cwd and not cwd.startswith("/")):
        raise Refused("cwd must be an absolute path")
    env = req.get("env") or {}
    if not (isinstance(env, dict) and len(env) <= 200):
        raise Refused("env must be an object")
    for k, v in env.items():
        if not (ENV_KEY_RE.match(k) and isinstance(v, str) and len(v) <= 16384 and "\0" not in v):
            raise Refused(f"bad environment variable {k!r}")
    geometry = req.get("geometry") or "1600x900"
    if not GEOMETRY_RE.match(geometry):
        raise Refused("geometry must look like 1600x900")
    return argv, cwd, {k: v for k, v in env.items() if k not in ENV_DROP}, geometry


def valid_id(value, what):
    if not (isinstance(value, str) and ID_RE.match(value)):
        raise Refused(f"invalid {what}")
    return value


def new_id():
    return "".join(secrets.choice("abcdefghijklmnopqrstuvwxyz0123456789") for _ in range(12))


def image_credentials_ok(img_dir, uid):
    """Every uid/gid stored in the image's process cores must be the caller's.
    (Images are root-only already; this is a second line of defence.)"""
    import pycriu  # shipped with criu
    found = False

    def walk(node):
        nonlocal found
        if isinstance(node, dict):
            for key in ("uid", "euid", "suid", "fsuid"):
                if key in node:
                    found = True
                    if int(node[key]) != uid:
                        return False
            return all(walk(v) for v in node.values())
        if isinstance(node, list):
            return all(walk(v) for v in node)
        return True

    for f in sorted(Path(img_dir).glob("core-*.img")):
        with open(f, "rb") as fh:
            if not walk(pycriu.images.load(fh)):
                return False
    return found


# ---------------------------------------------------------------- the helper
class Helper:
    def __init__(self, state_base=STATE_BASE, run_base=RUN_BASE, root_uid=0, run=subprocess.run):
        self.state_base = Path(state_base)
        self.run_base = Path(run_base)
        self.root_uid = root_uid  # owner the image directories must have (root in production)
        self.run = run
        self.lock = threading.Lock()

    # -- storage
    def user_dir(self, uid):
        d = self.state_base / str(uid)
        for sub in ("sessions", "images"):
            (d / sub).mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(d, 0o700)
        return d

    def sessions(self, uid):
        out = {}
        for f in (self.user_dir(uid) / "sessions").glob("*.json"):
            try:
                s = json.loads(f.read_text())
            except (OSError, ValueError):
                continue
            if self.alive(s):
                out[s["sid"]] = s
            else:  # ended, or its first process was killed: nothing may outlive it
                f.unlink(missing_ok=True)
                self.clean_up(uid, s)
        return out

    def session(self, uid, sid):
        s = self.sessions(uid).get(valid_id(sid, "session id"))
        if not s:
            raise Refused("no such running session")
        return s

    @staticmethod
    def alive(s):
        return start_time(s["pid"]) == s["start_time"]

    def save_session(self, uid, s):
        f = self.user_dir(uid) / "sessions" / f"{s['sid']}.json"
        f.write_text(json.dumps(s))
        os.chmod(f, 0o600)

    def image_dir(self, uid, iid):
        d = self.user_dir(uid) / "images" / valid_id(iid, "image id")
        st = d.lstat() if d.exists() else None
        if st is None:
            raise Refused("no such image")
        if not d.is_dir() or d.is_symlink() or st.st_uid != self.root_uid or st.st_mode & 0o077:
            raise Refused("image directory has unexpected owner or permissions")
        return d

    def images(self, uid):
        out = []
        for d in sorted((self.user_dir(uid) / "images").iterdir()):
            try:
                meta = json.loads((d / "meta.json").read_text())
            except (OSError, ValueError):
                continue
            out.append(meta)
        return out

    # -- operations
    def handle(self, uid, req):
        if uid < MIN_UID:
            raise Refused("only regular users can use the helper")
        if not isinstance(req, dict):
            raise Refused("request must be a JSON object")
        op = req.get("op")
        ops = {"start": self.start, "freeze": self.freeze, "restore": self.restore, "stop": self.stop,
               "list": self.list, "delete": self.delete, "ping": lambda uid, req: {"version": 1}}
        if op not in ops:
            raise Refused(f"unknown operation {op!r}")
        with self.lock:
            return ops[op](uid, req)

    def list(self, uid, req):
        return {"sessions": list(self.sessions(uid).values()), "images": self.images(uid)}

    def free_display(self):
        used = {s["display"] for u in self.state_base.glob("*") if u.name.isdigit()
                for s in self.sessions(int(u.name)).values()}
        for n in DISPLAYS:
            if n not in used and not Path(f"/tmp/.X11-unix/X{n}").exists() and not Path(f"/tmp/.X{n}-lock").exists():
                return n
        raise Refused("no free display number")

    def run_dir(self, uid, sid):
        """Per-session runtime directory (virtual display socket, D-Bus), owned by the user."""
        d = self.run_base / str(uid) / sid
        (self.run_base / str(uid)).mkdir(parents=True, exist_ok=True)
        os.chmod(self.run_base / str(uid), 0o755)  # the service's umask (077) would hide it from the user
        d.mkdir(parents=True, exist_ok=True, mode=0o700)
        user = pwd.getpwuid(uid)
        os.chown(d, uid, user.pw_gid)
        os.chmod(d, 0o700)
        return d

    def start(self, uid, req):
        argv, cwd, env, geometry = validate_start(req)
        if len(self.sessions(uid)) >= MAX_SESSIONS:
            raise Refused(f"you already have {MAX_SESSIONS} sessions running")
        sid, display = new_id(), self.free_display()
        rdir = self.run_dir(uid, sid)
        (rdir / "dbus.conf").write_text(DBUS_CONFIG.format(dir=rdir))
        os.chmod(rdir / "dbus.conf", 0o644)  # read by the session, which runs as the user
        user = pwd.getpwuid(uid)
        env.update(SESSION_ENV)
        env.update({"HOME": user.pw_dir, "USER": user.pw_name, "LOGNAME": user.pw_name,
                    "SHELL": user.pw_shell, "DISPLAY": f":{display}", "XDG_RUNTIME_DIR": str(rdir),
                    "STATEID_SESSION": sid})
        env.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
        spec = {"argv": argv, "cwd": cwd or user.pw_dir, "env": env, "display": display,
                "geometry": geometry, "run_dir": str(rdir)}
        pid = spawn_session(uid, spec)
        s = {"sid": sid, "uid": uid, "pid": pid, "start_time": start_time(pid), "display": display,
             "argv": argv, "run_dir": str(rdir), "vnc_socket": str(rdir / "vnc.sock"),
             "created": time.time(), "restored_from": None}
        if s["start_time"] is None:
            raise Refused("the session exited immediately")
        self.save_session(uid, s)
        return s

    def freeze(self, uid, req):
        s = self.session(uid, req.get("sid"))
        pids = tree(s["pid"])
        check_owned(pids, uid)
        check_sandboxes(pids)
        imgs = self.images(uid)
        if len(imgs) >= MAX_IMAGES:
            raise Refused(f"you have {MAX_IMAGES} frozen images; delete some first")
        if sum(m.get("bytes", 0) for m in imgs) >= MAX_IMAGE_BYTES:
            raise Refused("your frozen images use the whole quota; delete some first")
        iid = new_id()
        d = self.user_dir(uid) / "images" / iid
        d.mkdir(mode=0o700)
        cmd = [CRIU, "dump", "-t", str(s["pid"]), "-D", str(d), "--ext-unix-sk", "--link-remap",
               "--ghost-limit", "64M", "-v2", "-o", "dump.log"]
        if req.get("leave_running", True):
            cmd.append("--leave-running")
        t0 = time.time()
        r = self.run(cmd, capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            tail = log_tail(d / "dump.log")
            shutil.rmtree(d, ignore_errors=True)
            raise Refused("freeze failed: " + tail)
        size = sum(f.stat().st_size for f in d.rglob("*") if f.is_file())
        meta = {"iid": iid, "uid": uid, "sid": s["sid"], "argv": s["argv"], "display": s["display"],
                "run_dir": s["run_dir"], "created": time.time(), "bytes": size,
                "seconds": round(time.time() - t0, 3)}
        (d / "meta.json").write_text(json.dumps(meta))
        if not req.get("leave_running", True):
            self.forget(uid, s)
        return meta

    def restore(self, uid, req):
        iid = valid_id(req.get("iid"), "image id")
        d = self.image_dir(uid, iid)
        meta = json.loads((d / "meta.json").read_text())
        if meta.get("uid") != uid:
            raise Refused("image does not belong to you")
        if not image_credentials_ok(d, uid):
            raise Refused("image credentials do not match you")
        display = meta["display"]
        for s in self.sessions(uid).values():
            if s["display"] == display:
                raise Refused(f"session {s['sid']} still uses display :{display}; stop it first")
        if display_in_use(display):
            raise Refused(f"display :{display} is in use by another program")
        for stale in (f"/tmp/.X11-unix/X{display}", f"/tmp/.X{display}-lock"):
            Path(stale).unlink(missing_ok=True)
        rdir = Path(meta["run_dir"])
        if rdir.parent.parent != self.run_base or rdir.parent.name != str(uid):
            raise Refused("image refers to an unexpected runtime directory")
        self.run_dir(uid, rdir.name)
        for sock in rdir.glob("*.sock"):
            sock.unlink(missing_ok=True)
        pidfile = d / "restored.pid"
        pidfile.unlink(missing_ok=True)
        r = self.run([CRIU, "restore", "-D", str(d), "-d", "--ext-unix-sk", "--pidfile", str(pidfile),
                      "-v2", "-o", "restore.log"], capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            raise Refused("restore failed: " + log_tail(d / "restore.log"))
        pid = int(pidfile.read_text().strip())
        check_owned(tree(pid), uid)
        s = {"sid": rdir.name, "uid": uid, "pid": pid, "start_time": start_time(pid), "display": display,
             "argv": meta["argv"], "run_dir": str(rdir), "vnc_socket": str(rdir / "vnc.sock"),
             "created": time.time(), "restored_from": iid}
        self.save_session(uid, s)
        return s

    def stop(self, uid, req):
        s = self.session(uid, req.get("sid"))
        for p in reversed(killable(tree(s["pid"]), uid)):
            try:
                os.kill(p, signal.SIGKILL)
            except ProcessLookupError:
                pass
        for _ in range(50):
            if not self.alive(s):
                break
            time.sleep(0.05)
        self.clean_up(uid, s)
        return {"stopped": s["sid"]}

    def forget(self, uid, s):
        (self.user_dir(uid) / "sessions" / f"{s['sid']}.json").unlink(missing_ok=True)
        for stale in (f"/tmp/.X11-unix/X{s['display']}", f"/tmp/.X{s['display']}-lock"):
            if not display_in_use(s["display"]):
                Path(stale).unlink(missing_ok=True)

    def clean_up(self, uid, s):
        """After a session ended: stop its leftover processes and drop its runtime directory."""
        end_session_processes(s["sid"], uid, grace=0.5)
        self.forget(uid, s)
        self.drop_run_dir(uid, s["sid"])

    def sweep(self):
        """At start-up: end what sessions that are gone left behind (processes, runtime directories)."""
        for u in self.run_base.glob("*"):
            if not (u.name.isdigit() and int(u.name) >= MIN_UID) or u.is_symlink():
                continue
            uid = int(u.name)
            live = self.sessions(uid)
            for d in u.iterdir():
                if d.name not in live and ID_RE.match(d.name):
                    end_session_processes(d.name, uid, grace=0.5)
                    self.drop_run_dir(uid, d.name)

    def drop_run_dir(self, uid, sid):
        """Remove a session's runtime directory unless a frozen image still needs it (its program
        has files there open, e.g. session.log, which restoring reopens)."""
        if any(m.get("run_dir") == str(self.run_base / str(uid) / sid) for m in self.images(uid)):
            return
        d = self.run_base / str(uid) / valid_id(sid, "session id")
        if d.is_dir() and not d.is_symlink():  # the parent belongs to root, so the user cannot swap it
            shutil.rmtree(d, ignore_errors=True)

    def delete(self, uid, req):
        d = self.image_dir(uid, valid_id(req.get("iid"), "image id"))
        try:
            run_dir = Path(json.loads((d / "meta.json").read_text())["run_dir"])
        except (OSError, ValueError, KeyError, TypeError):
            run_dir = None
        shutil.rmtree(d)
        if run_dir and run_dir.parent == self.run_base / str(uid) and not any(
                s["sid"] == run_dir.name for s in self.sessions(uid).values()):
            self.drop_run_dir(uid, run_dir.name)
        return {"deleted": d.name}


def display_in_use(n):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(0.5)
    try:
        s.connect(f"/tmp/.X11-unix/X{n}")
        return True
    except OSError:
        return False
    finally:
        s.close()


def log_tail(path, lines=6):
    try:
        text = Path(path).read_text(errors="replace").splitlines()
    except OSError:
        return "(no log)"
    errors = [ln for ln in text if "Error" in ln] or text
    return " | ".join(errors[-lines:])


# ---------------------------------------------------------------- starting a session
def spawn_session(uid, spec):
    """Start `--session-init` as the user (never as root) in a new session; returns the PID of the
    session's first process. The launcher forks once and reports the PID, so the session is not
    a child of this (multi-threaded) helper."""
    user = pwd.getpwuid(uid)
    r = subprocess.run([PYTHON, "-I", os.path.abspath(__file__), "--session-init", json.dumps(spec)],
                       user=uid, group=user.pw_gid, extra_groups=os.getgrouplist(user.pw_name, user.pw_gid),
                       env={"PATH": "/usr/bin:/bin"}, cwd="/", stdin=subprocess.DEVNULL,
                       capture_output=True, text=True, timeout=20, start_new_session=True)
    try:
        pid = int(r.stdout.strip())
    except ValueError:
        raise Refused("could not start the session") from None
    deadline = time.time() + 10  # until the virtual display is up
    while time.time() < deadline and not Path(f"/tmp/.X11-unix/X{spec['display']}").exists():
        if start_time(pid) is None:
            raise Refused("the session exited while starting")
        time.sleep(0.05)
    return pid


PR_SET_CHILD_SUBREAPER = 36


def session_init(spec):
    """First process of a session (runs as the user): virtual display + program. It adopts every
    orphan of the session (child subreaper), so the whole session stays one process tree that
    CRIU can freeze, and it exits when the program exits."""
    if os.getuid() == 0 or os.geteuid() == 0:
        sys.exit("the session must not run as root")
    child = os.fork()
    if child:  # the launcher: report the session's PID and go
        print(child, flush=True)
        os._exit(0)
    os.setsid()  # CRIU freezes a process tree whose root leads its own session
    rdir, display = spec["run_dir"], spec["display"]
    # the session's own messages and its programs' output, readable by the user for troubleshooting
    log = os.open(f"{rdir}/session.log", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    os.dup2(os.open("/dev/null", os.O_RDONLY), 0)
    os.dup2(log, 1)
    os.dup2(log, 2)
    import ctypes
    ctypes.CDLL(None, use_errno=True).prctl(PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0)
    sid = spec["env"]["STATEID_SESSION"]
    # every process of the session carries its id, so nothing is left behind when it ends
    base_env = {"PATH": "/usr/bin:/bin", "HOME": spec["env"]["HOME"], "STATEID_SESSION": sid}
    xvnc = subprocess.Popen(
        [XVNC, f":{display}", "-geometry", spec["geometry"], "-depth", "24", "-rfbport", "-1",
         "-rfbunixpath", f"{rdir}/vnc.sock", "-rfbunixmode", "0600", "-SecurityTypes", "None",
         "-AlwaysShared", "-AcceptSetDesktopSize", "-nolisten", "tcp", "-extension", "MIT-SHM",
         # no GPU: an open /dev/dri render node cannot be checkpointed, and without GLX no program
         # creates a Mesa context (which opens /dev/udmabuf)
         "-rendernode", "none", "-extension", "GLX",
         "-desktop", f"{os.path.basename(spec['argv'][0])} (stateid {os.path.basename(rdir)})"],
        stdin=subprocess.DEVNULL, env=base_env)
    for _ in range(200):
        if os.path.exists(f"/tmp/.X11-unix/X{display}"):
            break
        time.sleep(0.05)
    wm = None
    if os.path.exists(WM):  # part of the session, so it is frozen and restored with the program
        wm = subprocess.Popen([WM, "-use_titlebar", "no", "-display", f":{display}"],
                              stdin=subprocess.DEVNULL, env=base_env)
    cwd = spec["cwd"] if os.path.isdir(spec["cwd"]) else spec["env"]["HOME"]
    app = subprocess.Popen([DBUS_RUN_SESSION, f"--config-file={rdir}/dbus.conf", "--", *spec["argv"]],
                           cwd=cwd, env=spec["env"], stdin=subprocess.DEVNULL)

    procs = [p for p in (app, wm, xvnc) if p]

    def finish(*_):
        for p in procs:  # a clean stop lets Xvnc remove its socket and lock files
            try:
                p.terminate()
            except OSError:
                pass
        deadline = time.time() + 2
        while time.time() < deadline and any(p.poll() is None for p in procs):
            time.sleep(0.05)
        for p in procs:
            if p.poll() is None:
                p.kill()
        end_session_processes(sid, os.getuid(), grace=1.0)  # what the program left running (D-Bus, helpers)
        os._exit(0)

    signal.signal(signal.SIGTERM, finish)
    # a stray signal must not end the session and leave its display and program orphaned
    for sig in (signal.SIGHUP, signal.SIGINT, signal.SIGUSR1, signal.SIGUSR2, signal.SIGPIPE):
        signal.signal(sig, signal.SIG_IGN)
    while True:  # reap every orphan; the session ends with its program
        try:
            pid, status = os.wait()
        except ChildProcessError:
            finish()
        except InterruptedError:
            continue
        if pid == app.pid:
            print(f"stateid session: program exited ({os.waitstatus_to_exitcode(status)})", flush=True)
            finish()
        elif pid == xvnc.pid:
            print("stateid session: virtual display exited", flush=True)


# ---------------------------------------------------------------- server
def serve(helper, path=SOCKET_PATH):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).unlink(missing_ok=True)
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(path)
    os.chmod(path, 0o666)  # any local user may ask; each request is checked against SO_PEERCRED
    srv.listen(16)
    print(f"stateid-helperd listening on {path}", file=sys.stderr, flush=True)
    while True:
        conn, _ = srv.accept()
        threading.Thread(target=answer, args=(helper, conn), daemon=True).start()


def answer(helper, conn):
    with conn:
        conn.settimeout(300)
        try:
            pid, uid, gid = struct.unpack("3i", conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED,
                                                                struct.calcsize("3i")))
            data = b""
            while not data.endswith(b"\n"):
                chunk = conn.recv(65536)
                if not chunk:
                    break
                data += chunk
                if len(data) > MAX_REQUEST:
                    raise Refused("request too large")
            req = json.loads(data or b"null")
            reply = {"ok": True, "result": helper.handle(uid, req)}
            print(f"uid {uid}: {req.get('op')} ok", file=sys.stderr, flush=True)
        except Refused as e:
            reply = {"ok": False, "error": str(e)}
            print(f"refused: {e}", file=sys.stderr, flush=True)
        except Exception as e:  # never let one request take the helper down
            reply = {"ok": False, "error": f"internal error: {type(e).__name__}"}
            print(f"internal error: {e!r}", file=sys.stderr, flush=True)
        try:
            conn.sendall(json.dumps(reply).encode() + b"\n")
        except OSError:
            pass


def main():
    if len(sys.argv) == 3 and sys.argv[1] == "--session-init":
        session_init(json.loads(sys.argv[2]))
        return
    if os.geteuid() != 0:
        sys.exit("stateid-helperd must run as root (it is started by systemd)")
    os.umask(0o077)
    helper = Helper()
    helper.sweep()
    serve(helper)


if __name__ == "__main__":
    main()
