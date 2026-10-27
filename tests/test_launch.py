"""Reopening closed apps: AppImages, relative paths, programs that are gone. No display needed."""
import os
import subprocess
import sys
import time

from stateid import core
from stateid.adapters.base import Adapter
from stateid.procs import launch_argv


def test_appimage_is_saved_as_the_appimage_file(tmp_path):
    env = dict(os.environ, APPIMAGE="/opt/pt/packettracer.AppImage", OWD=str(tmp_path))
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(5)"], env=env)
    for _ in range(100):  # until the child has exec'd and carries its own environment
        if b"APPIMAGE=" in open(f"/proc/{p.pid}/environ", "rb").read():
            break
        time.sleep(0.01)
    try:
        cmd, cwd = launch_argv(p.pid, ["./PacketTracer", "lab.pkt"], "/tmp/.mount_packetXYZ/opt/pt/bin")
    finally:
        p.kill()
    assert cmd == ["/opt/pt/packettracer.AppImage", "lab.pkt"] and cwd == str(tmp_path)


def test_relative_program_becomes_absolute():
    cmd, _ = launch_argv(os.getpid(), ["./tool", "--x"], "/somewhere")
    assert os.path.isabs(cmd[0]) and cmd[1:] == ["--x"]


def test_runnable():
    assert core._runnable(["sh", "-c", "true"])
    assert not core._runnable(["./PacketTracer"])
    assert not core._runnable(["/tmp/.mount_gone/PacketTracer"])
    assert not core._runnable(["/usr/bin/ptyxis", "--gapplication-service"])
    assert not core._runnable([])


def test_missing_program_is_a_note_not_a_crash(monkeypatch):
    monkeypatch.setattr(core, "desktop_command", lambda *a: None)
    st = {"window": {"app_id": "window:15", "wm_class": "PacketTracer", "title": "Cisco Packet Tracer"},
          "files": [], "cwd": "/tmp/.mount_gone/opt/pt/bin", "command": ["./PacketTracer"], "restore_cmd": None}
    launched, notes = core._launch(st, Adapter())
    assert launched == [] and "cannot reopen" in notes[0]


def test_falls_back_to_the_desktop_launcher(monkeypatch):
    spawned = []
    monkeypatch.setattr(core, "desktop_command", lambda app_id, docs, wm_class=None: ["sh", "-c", "true"])
    monkeypatch.setattr(core, "spawn", lambda cmd, cwd=None: spawned.append(cmd) or " ".join(cmd))
    st = {"window": {"app_id": "window:15", "wm_class": "PacketTracer", "title": "Cisco Packet Tracer"},
          "files": [], "cwd": "/tmp/.mount_gone", "command": ["./PacketTracer"], "restore_cmd": None}
    launched, _ = core._launch(st, Adapter())
    assert spawned == [["sh", "-c", "true"]] and launched
