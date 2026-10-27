"""Security checks of the root freeze helper. Runs without root: CRIU is replaced by a fake."""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

spec = importlib.util.spec_from_file_location(
    "stateid_helperd", Path(__file__).resolve().parents[1] / "helper" / "stateid_helperd.py")
hd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hd)

UID = os.getuid()
pytestmark = pytest.mark.skipif(UID < hd.MIN_UID, reason="needs a regular user account")


class FakeCriu:
    """Stands in for criu: records calls; dump writes a core image, restore writes the pidfile."""

    def __init__(self, restored_pid=None, fail=False):
        self.calls, self.restored_pid, self.fail = [], restored_pid, fail

    def __call__(self, cmd, **kw):
        self.calls.append(cmd)
        d = Path(cmd[cmd.index("-D") + 1])
        if self.fail:
            (d / ("dump.log" if cmd[1] == "dump" else "restore.log")).write_text("Error (x): it broke\n")
            return SimpleNamespace(returncode=1)
        if cmd[1] == "dump":
            (d / "core-1.img").write_bytes(b"x" * 100)
        else:
            Path(cmd[cmd.index("--pidfile") + 1]).write_text(str(self.restored_pid))
        return SimpleNamespace(returncode=0)


@pytest.fixture
def helper(tmp_path, monkeypatch):
    monkeypatch.setattr(hd, "image_credentials_ok", lambda d, uid: True)
    monkeypatch.setattr(hd, "display_in_use", lambda n: False)
    h = hd.Helper(state_base=tmp_path / "state", run_base=tmp_path / "run", root_uid=UID, run=FakeCriu())
    return h


@pytest.fixture
def sleeper():
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    yield p
    p.kill()
    p.wait()


def register(helper, pid, sid="aaaaaaaaaaaa", display=150):
    s = {"sid": sid, "uid": UID, "pid": pid, "start_time": hd.start_time(pid), "display": display,
         "argv": ["x"], "run_dir": str(helper.run_base / str(UID) / sid), "vnc_socket": "", "created": 0,
         "restored_from": None}
    helper.save_session(UID, s)
    return s


# ---------------------------------------------------------------- who may ask
def test_root_and_system_accounts_are_refused(helper):
    for uid in (0, 1, 999):
        with pytest.raises(hd.Refused, match="regular users"):
            helper.handle(uid, {"op": "list"})


def test_unknown_operations_are_refused(helper):
    for req in ({"op": "exec", "argv": ["sh"]}, {"op": "run_as_root"}, [], "list", None):
        with pytest.raises(hd.Refused):
            helper.handle(UID, req)


# ---------------------------------------------------------------- input validation
@pytest.mark.parametrize("req", [
    {"argv": []}, {"argv": "gedit"}, {"argv": ["a\0b"]}, {"argv": ["x"] * 65},
    {"argv": ["x"], "cwd": "relative/dir"}, {"argv": ["x"], "env": {"BAD KEY": "1"}},
    {"argv": ["x"], "env": {"K": 1}}, {"argv": ["x"], "geometry": "huge"},
])
def test_bad_start_requests(req):
    with pytest.raises(hd.Refused):
        hd.validate_start(req)


def test_session_env_cannot_point_outside_the_frozen_tree():
    _, _, env, _ = hd.validate_start({"argv": ["x"], "env": {
        "WAYLAND_DISPLAY": "wayland-0", "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1/bus",
        "HOME": "/root", "LANG": "tr_TR.UTF-8"}})
    assert env == {"LANG": "tr_TR.UTF-8"}


def test_software_rendering_is_kept_off_udmabuf_when_the_library_is_installed(helper, sleeper, monkeypatch):
    specs = []
    monkeypatch.setattr(hd, "spawn_session", lambda uid, spec: specs.append(spec) or sleeper.pid)
    req = {"op": "start", "argv": ["x"], "env": {"LD_PRELOAD": "mine.so", "VK_ADD_DRIVER_FILES": "gpu.json"}}
    for installed in (False, True):
        monkeypatch.setattr(hd, "root_owned", lambda p: installed)
        helper.handle(UID, req)
    without, with_ = specs
    assert not without["glx"] and without["env"]["LD_PRELOAD"] == "mine.so"
    assert with_["glx"] and with_["env"]["LD_PRELOAD"] == f"{hd.NODEV} mine.so"
    if os.path.exists(hd.LAVAPIPE_ICD):
        assert with_["env"]["VK_DRIVER_FILES"] == hd.LAVAPIPE_ICD and "VK_ADD_DRIVER_FILES" not in with_["env"]


def test_a_library_the_user_could_change_is_not_preloaded(tmp_path):
    lib = tmp_path / "stateid_nodev.so"
    lib.touch()
    assert not hd.root_owned(lib)
    assert hd.root_owned("/usr/bin/env")


@pytest.mark.parametrize("bad", ["../../etc", "AAAAAAAAAAAA", "short", "", None, 5, "aaaaaaaaaaaa/.."])
def test_ids_are_strict(bad):
    with pytest.raises(hd.Refused):
        hd.valid_id(bad, "id")


# ---------------------------------------------------------------- ownership of the processes
def test_own_processes_pass(sleeper):
    hd.check_owned(hd.tree(sleeper.pid), UID)


def test_foreign_or_privileged_processes_are_refused():
    me = hd.proc_status(os.getpid())
    other = dict(me, Uid="0\t0\t0\t0")
    with pytest.raises(hd.Refused, match="does not belong"):
        hd.check_owned([1], UID, status=lambda p: other)
    setuid_like = dict(me, Uid=f"{UID}\t0\t{UID}\t{UID}")  # effective uid root
    with pytest.raises(hd.Refused):
        hd.check_owned([1], UID, status=lambda p: setuid_like)
    capable = dict(me, CapEff="0000000000200000")
    with pytest.raises(hd.Refused, match="capabilities"):
        hd.check_owned([1], UID, status=lambda p: capable)
    other_group = dict(me, Gid="0\t0\t0\t0")
    with pytest.raises(hd.Refused, match="group"):
        hd.check_owned([1], UID, status=lambda p: other_group)


def test_stop_kills_capable_processes_of_the_user_but_never_foreign_ones():
    me = hd.proc_status(os.getpid())
    table = {1: me, 2: dict(me, CapEff="0000000000003000"), 3: dict(me, Uid=f"{UID}\t0\t{UID}\t{UID}"),
             4: dict(me, Uid="0\t0\t0\t0"), 5: None}
    assert hd.killable([1, 2, 3, 4, 5], UID, status=table.get) == [1, 2]


def test_processes_in_a_nested_pid_namespace_are_refused_with_a_reason(sleeper):
    hd.check_sandboxes(hd.tree(sleeper.pid))
    me = hd.proc_status(os.getpid())
    sandboxed = dict(me, Name="bwrap", NSpid=f"{os.getpid()}\t1")
    with pytest.raises(hd.Refused, match="sandbox"):
        hd.check_sandboxes([1], status=lambda p: sandboxed)


def test_init_process_of_the_system_is_refused():
    with pytest.raises(hd.Refused):
        hd.check_owned([1], UID)


def test_reused_pid_is_not_the_session(helper, sleeper):
    s = register(helper, sleeper.pid)
    assert helper.sessions(UID)
    s["start_time"] -= 1  # same PID, different process
    helper.save_session(UID, s)
    assert helper.sessions(UID) == {}
    with pytest.raises(hd.Refused, match="no such running session"):
        helper.handle(UID, {"op": "freeze", "sid": s["sid"]})


def test_sessions_of_another_user_are_invisible(helper, sleeper):
    register(helper, sleeper.pid)
    other_dir = helper.state_base / str(UID + 1)
    (other_dir / "sessions").mkdir(parents=True)
    with pytest.raises(hd.Refused):
        helper.session(UID + 1, "aaaaaaaaaaaa")


# ---------------------------------------------------------------- freeze, restore, delete
def test_freeze_writes_a_private_image(helper, sleeper):
    s = register(helper, sleeper.pid)
    meta = helper.handle(UID, {"op": "freeze", "sid": s["sid"]})
    d = helper.state_base / str(UID) / "images" / meta["iid"]
    assert oct(d.stat().st_mode & 0o777) == "0o700" and meta["bytes"] > 0
    cmd = helper.run.calls[-1]
    assert cmd[:4] == [hd.CRIU, "dump", "-t", str(sleeper.pid)] and "--leave-running" in cmd


def test_failed_freeze_leaves_nothing(helper, sleeper):
    s = register(helper, sleeper.pid)
    helper.run = FakeCriu(fail=True)
    with pytest.raises(hd.Refused, match="it broke"):
        helper.handle(UID, {"op": "freeze", "sid": s["sid"]})
    assert list((helper.state_base / str(UID) / "images").iterdir()) == []


def test_quota(helper, sleeper, monkeypatch):
    s = register(helper, sleeper.pid)
    monkeypatch.setattr(hd, "MAX_IMAGES", 1)
    helper.handle(UID, {"op": "freeze", "sid": s["sid"]})
    with pytest.raises(hd.Refused, match="delete some"):
        helper.handle(UID, {"op": "freeze", "sid": s["sid"]})


def test_restore_registers_the_new_session(helper, sleeper):
    s = register(helper, sleeper.pid)
    meta = helper.handle(UID, {"op": "freeze", "sid": s["sid"]})
    helper.handle(UID, {"op": "stop", "sid": s["sid"]})
    assert sleeper.wait(timeout=5) is not None
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        helper.run.restored_pid = other.pid
        new = helper.handle(UID, {"op": "restore", "iid": meta["iid"]})
        assert new["pid"] == other.pid and new["restored_from"] == meta["iid"]
        assert set(helper.sessions(UID)) == {s["sid"]}
    finally:
        other.kill()


def test_restore_puts_back_the_run_dir_a_reboot_emptied(helper, sleeper, tmp_path):
    s = register(helper, sleeper.pid)
    rdir = helper.run_dir(UID, s["sid"])
    (rdir / "session.log").write_text("what the session wrote\n")
    os.chmod(rdir / "session.log", 0o600)
    (rdir / "dbus-abc").mkdir(mode=0o700)
    secret = tmp_path / "elsewhere"
    secret.write_text("not for the image")
    (rdir / "link").symlink_to(secret)
    os.link(secret, rdir / "second-name")  # a hard link to a file outside
    meta = helper.handle(UID, {"op": "freeze", "sid": s["sid"]})
    helper.handle(UID, {"op": "stop", "sid": s["sid"]})
    assert sleeper.wait(timeout=5) is not None
    shutil.rmtree(helper.run_base)  # as after a reboot
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        helper.run.restored_pid = other.pid
        helper.handle(UID, {"op": "restore", "iid": meta["iid"]})
    finally:
        other.kill()
        other.wait()
    assert (rdir / "session.log").read_text() == "what the session wrote\n"
    assert (rdir / "session.log").stat().st_mode & 0o777 == 0o600
    assert (rdir / "dbus-abc").is_dir()
    assert sorted(p.name for p in rdir.iterdir()) == ["dbus-abc", "session.log"]


def test_restore_keeps_what_is_in_the_run_dir(helper, sleeper):
    s = register(helper, sleeper.pid)
    rdir = helper.run_dir(UID, s["sid"])
    (rdir / "session.log").write_text("old\n")
    meta = helper.handle(UID, {"op": "freeze", "sid": s["sid"]})
    helper.handle(UID, {"op": "stop", "sid": s["sid"]})
    assert sleeper.wait(timeout=5) is not None
    (rdir / "session.log").write_text("old\nnewer\n")
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        helper.run.restored_pid = other.pid
        helper.handle(UID, {"op": "restore", "iid": meta["iid"]})
    finally:
        other.kill()
        other.wait()
    assert (rdir / "session.log").read_text() == "old\nnewer\n"


def test_restore_refuses_a_busy_display(helper, sleeper):
    s = register(helper, sleeper.pid)
    meta = helper.handle(UID, {"op": "freeze", "sid": s["sid"]})
    with pytest.raises(hd.Refused, match="still uses display"):
        helper.handle(UID, {"op": "restore", "iid": meta["iid"]})


def test_tampered_or_foreign_images_are_refused(helper, sleeper, monkeypatch):
    s = register(helper, sleeper.pid)
    meta = helper.handle(UID, {"op": "freeze", "sid": s["sid"]})
    d = helper.state_base / str(UID) / "images" / meta["iid"]
    helper.handle(UID, {"op": "stop", "sid": s["sid"]})
    os.chmod(d, 0o755)  # writable/readable by others: not trusted
    with pytest.raises(hd.Refused, match="owner or permissions"):
        helper.handle(UID, {"op": "restore", "iid": meta["iid"]})
    os.chmod(d, 0o700)
    (d / "meta.json").write_text(json.dumps(dict(meta, uid=UID + 1)))
    with pytest.raises(hd.Refused, match="does not belong"):
        helper.handle(UID, {"op": "restore", "iid": meta["iid"]})
    (d / "meta.json").write_text(json.dumps(dict(meta, run_dir="/etc/evil")))
    with pytest.raises(hd.Refused, match="runtime directory"):
        helper.handle(UID, {"op": "restore", "iid": meta["iid"]})
    (d / "meta.json").write_text(json.dumps(meta))
    monkeypatch.setattr(hd, "image_credentials_ok", lambda d, uid: False)
    with pytest.raises(hd.Refused, match="credentials"):
        helper.handle(UID, {"op": "restore", "iid": meta["iid"]})


def test_image_owned_by_the_user_is_not_trusted(tmp_path, sleeper, monkeypatch):
    """In production images must be root's; a directory the user owns could have been edited."""
    monkeypatch.setattr(hd, "display_in_use", lambda n: False)
    h = hd.Helper(state_base=tmp_path / "s", run_base=tmp_path / "r", root_uid=0, run=FakeCriu())
    s = register(h, sleeper.pid)
    meta = h.handle(UID, {"op": "freeze", "sid": s["sid"]})
    with pytest.raises(hd.Refused, match="owner or permissions"):
        h.handle(UID, {"op": "restore", "iid": meta["iid"]})


def test_run_dir_is_reachable_by_the_user_only(helper):
    old = os.umask(0o077)  # as under systemd's UMask=0077
    try:
        d = helper.run_dir(UID, "bbbbbbbbbbbb")
    finally:
        os.umask(old)
    assert oct(d.parent.stat().st_mode & 0o777) == "0o755"
    assert oct(d.stat().st_mode & 0o777) == "0o700" and d.stat().st_uid == UID


def test_delete(helper, sleeper):
    s = register(helper, sleeper.pid)
    meta = helper.handle(UID, {"op": "freeze", "sid": s["sid"]})
    helper.handle(UID, {"op": "delete", "iid": meta["iid"]})
    assert helper.handle(UID, {"op": "list"})["images"] == []


def test_image_credentials_check_reads_criu_images(tmp_path):
    pycriu = pytest.importorskip("pycriu")
    core = {"magic": "CORE", "entries": [{"mtype": "X86_64", "tc": {"task_state": 1, "exit_code": 0, "personality": 0, "flags": 0, "blk_sigset": 0, "comm": "x"},
        "thread_core": {"futex_rla": 0, "futex_rla_len": 0,
        "creds": {"uid": UID, "gid": UID, "euid": UID, "egid": UID, "suid": UID, "sgid": UID,
                  "fsuid": UID, "fsgid": UID, "cap_inh": [0, 0], "cap_prm": [0, 0], "cap_eff": [0, 0], "cap_bnd": [0, 0],
                  "secbits": 0}}}]}
    with open(tmp_path / "core-1.img", "wb") as f:
        pycriu.images.dump(core, f)
    assert hd.image_credentials_ok(tmp_path, UID)
    assert not hd.image_credentials_ok(tmp_path, UID + 1)


def tagged(sid, **env):
    """A process of session `sid` that left its process tree (like a daemon of the program)."""
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                            env={**os.environ, "STATEID_SESSION": sid, **env})


def test_ended_session_leaves_no_processes_or_run_dir(helper, sleeper):
    s = register(helper, sleeper.pid)
    helper.run_dir(UID, s["sid"])
    left, other = tagged(s["sid"]), tagged("bbbbbbbbbbbb")
    try:
        sleeper.kill()
        sleeper.wait()
        assert helper.sessions(UID) == {}  # noticing the end cleans up
        assert left.wait(timeout=5) is not None
        assert other.poll() is None  # another session's process is untouched
        assert not (helper.run_base / str(UID) / s["sid"]).exists()
    finally:
        for p in (left, other):
            p.kill()
            p.wait()


def test_run_dir_stays_while_an_image_needs_it(helper, sleeper):
    s = register(helper, sleeper.pid)
    rdir = helper.run_dir(UID, s["sid"])
    meta = helper.handle(UID, {"op": "freeze", "sid": s["sid"]})
    helper.handle(UID, {"op": "stop", "sid": s["sid"]})
    assert rdir.exists()  # restoring reopens files the program had open there
    helper.handle(UID, {"op": "delete", "iid": meta["iid"]})
    assert not rdir.exists()


def test_sweep_ends_what_a_killed_helper_left(helper):
    rdir = helper.run_dir(UID, "cccccccccccc")
    left = tagged("cccccccccccc")
    try:
        helper.sweep()
        assert left.wait(timeout=5) is not None and not rdir.exists()
    finally:
        left.kill()
        left.wait()


def test_nodev_library_refuses_only_udmabuf(tmp_path):
    cc = shutil.which("cc") or shutil.which("gcc")
    if not cc:
        pytest.skip("no C compiler")
    lib = tmp_path / "nodev.so"
    subprocess.run([cc, "-O2", "-shared", "-fPIC", "-o", str(lib), str(Path(hd.__file__).with_name("stateid_nodev.c")),
                    "-ldl"], check=True)
    probe = ("import errno, os\n"
             "for p in ('/dev/udmabuf', '/dev/null'):\n"
             "    try: os.close(os.open(p, os.O_RDWR)); print(p, 'open')\n"
             "    except OSError as e: print(p, errno.errorcode[e.errno])\n")
    out = subprocess.run([sys.executable, "-c", probe], env=dict(os.environ, LD_PRELOAD=str(lib)),
                         capture_output=True, text=True, check=True).stdout
    assert out.splitlines() == ["/dev/udmabuf EACCES", "/dev/null open"]
