"""Terminal tabs: which commands are resumed on restore. No display needed."""
import json
import os

import pytest

from stateid.adapters import terminal
from stateid.adapters.terminal import claude_session, resume_argv, tab_command

SID = "56c49f5c-1685-42cd-90f8-f9b5f861f690"


@pytest.mark.parametrize("running, session, expected", [
    ("claude", SID, ["claude", "--resume", SID]),
    (f"claude --resume {SID}", None, ["claude", "--resume", SID]),
    (f"claude --dangerously-skip-permissions --resume {SID}", None,
     ["claude", "--dangerously-skip-permissions", "--resume", SID]),
    (f"/home/me/.local/bin/claude -c --model opus", SID, ["claude", "--model", "opus", "--resume", SID]),
    ("claude", None, None),                    # no session id: nothing to resume
    ("claude -p 'summarise this'", SID, None),  # one-shot run
    ("vim notes.txt", SID, None),              # not Claude Code
    ("claude --resume ../../etc", None, None),  # only real session ids
])
def test_resume_argv(running, session, expected):
    assert resume_argv(running, session) == expected


def test_tab_command_keeps_a_shell_after_claude_exits():
    assert tab_command({"running": "claude", "claude_session": SID, "shell": "zsh"}) == \
        ["zsh", "-ic", f"claude --resume {SID}; exec zsh"]
    assert tab_command({"running": "claude", "claude_session": SID, "shell": "weird"})[0] == "bash"
    assert tab_command({"running": "bash", "shell": "bash"}) is None


def test_claude_session_reads_the_session_file(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    (tmp_path / "sessions").mkdir()
    pid = os.getpid()
    (tmp_path / "sessions" / f"{pid}.json").write_text(json.dumps({"sessionId": SID, "kind": "interactive"}))
    sid, argv = claude_session([999999999, pid])
    assert sid == SID and argv == terminal.cmdline(pid)
    (tmp_path / "sessions" / f"{pid}.json").write_text(json.dumps({"sessionId": "not-a-uuid"}))
    assert claude_session([pid]) == (None, None)
