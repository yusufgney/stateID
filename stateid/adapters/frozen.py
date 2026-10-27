"""Freezable sessions (`stateid run`): the window on the desktop is a VNC viewer showing a program
that runs on its own virtual display. Saving freezes the program's whole memory through the helper
(see core.freeze_sessions); restoring brings that frozen moment back, unsaved work included."""
import re

from .. import freeze
from .base import Adapter


def session_of(w):
    """The session id behind a viewer window, from the viewer's pid."""
    if not freeze.VIEWER_DIR.exists():
        return None
    return next((f.name for f in freeze.VIEWER_DIR.iterdir() if freeze.viewer_pid(f.name) == w["pid"]), None)


class FrozenSession(Adapter):
    pattern = re.compile(r"tigervnc", re.I)

    def capture(self, w, st):
        sid = session_of(w)
        if not sid:
            return
        running = {s["sid"]: s for s in freeze.sessions()["sessions"]}
        if sid in running:
            st["app_state"] = {"freeze_session": sid, "program": running[sid]["argv"]}

    def launch(self, st):
        a = st.get("app_state") or {}
        sid, iid = a.get("freeze_session"), a.get("frozen_image")
        if not iid:
            return []
        running = {s["sid"]: s for s in freeze.sessions()["sessions"]}
        if sid in running:  # still running with newer work: show it, never overwrite it
            freeze.show(running[sid])
            return [f"stateid sessions: showing running session {sid}"]
        s = freeze.restore(iid)
        return [f"stateid: frozen session {iid} restored (memory included) as {s['sid']}"]
