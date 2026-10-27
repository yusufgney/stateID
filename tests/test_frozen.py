"""Freezable sessions in save/restore, with the helper replaced by a fake. No display or root needed."""
import json

import pytest

from stateid import core, freeze, store
from stateid.adapters.frozen import FrozenSession


class FakeHelper:
    def __init__(self, running=(), images=("img000000001",)):
        self.running = {sid: {"sid": sid, "argv": ["gedit"], "display": 100, "pid": 1,
                              "vnc_socket": "/x"} for sid in running}
        self.images = list(images)
        self.calls = []

    def sessions(self):
        return {"sessions": list(self.running.values()), "images": [{"iid": i} for i in self.images]}

    def freeze(self, sid):
        self.calls.append(("freeze", sid))
        return {"iid": "img000000002", "bytes": 123}

    def restore(self, iid):
        self.calls.append(("restore", iid))
        return {"sid": "sess00000001"}

    def show(self, s):
        self.calls.append(("show", s["sid"]))

    def call(self, op, **kw):
        self.calls.append((op, kw))
        self.images = [i for i in self.images if i != kw.get("iid")]


@pytest.fixture
def helper(monkeypatch):
    h = FakeHelper()
    for name in ("sessions", "freeze", "restore", "show", "call"):
        monkeypatch.setattr(freeze, name, getattr(h, name))
    monkeypatch.setattr(freeze, "available", lambda: True)
    return h


def st(state=None):
    return {"app_state": state, "window": {"app_id": "xtigervncviewer.desktop"}}


def test_save_freezes_sessions_after_the_screenshots(helper):
    states = [st({"freeze_session": "sess00000001"}), st({"tabs": []}), st(None)]
    core.freeze_sessions(states)
    assert helper.calls == [("freeze", "sess00000001")]
    assert states[0]["app_state"]["frozen_image"] == "img000000002"


def test_a_failed_freeze_is_recorded_not_raised(helper, monkeypatch):
    def boom(sid):
        raise freeze.HelperError("quota")
    monkeypatch.setattr(freeze, "freeze", boom)
    states = [st({"freeze_session": "sess00000001"})]
    core.freeze_sessions(states)
    assert states[0]["app_state"]["freeze_error"] == "quota"


def test_restore_brings_back_a_stopped_session(helper):
    launched = FrozenSession().launch(st({"freeze_session": "sess00000001", "frozen_image": "img000000001"}))
    assert helper.calls == [("restore", "img000000001")] and launched


def test_restore_never_overwrites_a_running_session(helper):
    helper.running = FakeHelper(running=["sess00000001"]).running
    FrozenSession().launch(st({"freeze_session": "sess00000001", "frozen_image": "img000000001"}))
    assert helper.calls == [("show", "sess00000001")]


def test_nothing_to_restore_without_an_image(helper):
    assert FrozenSession().launch(st({"freeze_session": "sess00000001"})) == []


def test_deleting_a_state_deletes_its_frozen_images(helper, tmp_path, monkeypatch):
    monkeypatch.setattr(store, "STORE", tmp_path)
    (tmp_path / "DK-AAAAAAAA").mkdir()
    (tmp_path / "DK-AAAAAAAA" / "state.json").write_text(json.dumps({"windows": [
        st({"freeze_session": "s", "frozen_image": "img000000001"}), st({"tabs": []})]}))
    store.delete("DK-AAAAAAAA")
    assert ("delete", {"iid": "img000000001"}) in helper.calls and not (tmp_path / "DK-AAAAAAAA").exists()
