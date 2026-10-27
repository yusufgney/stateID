"""Privacy layer: redaction, matching, and what agents can see of saved states. No display needed."""
import json

import pytest

from stateid import core, privacy, store
from stateid.privacy import MASK, redact, redact_url


# ---------------------------------------------------------------- redaction
@pytest.mark.parametrize("secret", [
    "ghp_" + "a" * 36,
    "github_pat_" + "A1b2" * 10,
    "glpat-" + "x" * 20,
    "sk-ant-api03-" + "Z" * 40,
    "sk-proj-" + "q" * 30,
    "xoxb-1234567890-abcdefghij",
    "AIza" + "B" * 35,
    "AKIA" + "ABCDEFGHIJKLMNOP",
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U",
])
def test_known_token_formats_are_masked(secret):
    out = redact(f"export X={secret} # done")
    assert secret not in out and MASK in out


def test_key_value_keeps_the_key():
    assert redact("password=hunter2") == f"password={MASK}"
    assert redact('API_KEY: "abc123xyz"') == f"API_KEY: {MASK}"
    assert redact("db_password = s3cr3t; next") == f"db_password = {MASK}; next"


def test_private_key_block():
    text = "a\n-----BEGIN OPENSSH PRIVATE KEY-----\nAAAAB3NzaC1yc2E\n-----END OPENSSH PRIVATE KEY-----\nb"
    assert "AAAAB3" not in redact(text)


def test_bearer_header():
    assert "abcdefghijklmnopqrstuvwxyz" not in redact("Authorization: Bearer abcdefghijklmnopqrstuvwxyz")


@pytest.mark.parametrize("text", [
    "max_tokens=500",
    "The token count is fine",
    "author: Jane",
    "git commit -m 'fix tokenizer'",
    "ls -la ~/project && make test",
])
def test_ordinary_text_is_left_alone(text):
    assert redact(text) == text


def test_url_credentials_and_sensitive_params():
    url = "https://user:pa55@example.com/cb?code=XYZ&state=ok&access_token=abc#frag"
    out = redact_url(url)
    assert "pa55" not in out and "XYZ" not in out and "abc" not in out
    assert "state=ok" in out and "example.com" in out


def test_plain_url_unchanged():
    assert redact_url("https://web.whatsapp.com/") == "https://web.whatsapp.com/"


def test_redact_deep_walks_structures():
    data = {"a": ["password=x1", {"b": "ghp_" + "c" * 36}], "n": 3}
    out = privacy.redact_deep(data)
    assert out["n"] == 3 and MASK in out["a"][0] and out["a"][1]["b"] == MASK


# ---------------------------------------------------------------- matching
def test_matches_substring_and_glob():
    assert privacy.matches(["keepassxc"], "org.keepassxc.KeePassXC.desktop")
    assert privacy.matches(["*bank*"], None, "My Bank - Brave")
    assert privacy.matches(["(incognito)"], "New Tab - Google Chrome (Incognito)")
    assert not privacy.matches(["whatsapp"], "brave-browser.desktop", "Inbox - Brave")


def test_config_file_overrides_defaults(tmp_path, monkeypatch):
    cfg = tmp_path / "config.toml"
    cfg.write_text('[privacy]\nprivate = ["whatsapp"]\nagent_screenshots = false\n[storage]\nkeep_days = 7\n')
    monkeypatch.setattr(privacy, "CONFIG_PATH", cfg)
    c = privacy.load()
    assert c["privacy"]["private"] == ["whatsapp"]
    assert c["privacy"]["agent_screenshots"] is False
    assert "keepassxc" in c["privacy"]["exclude"]  # untouched defaults survive
    assert c["storage"]["keep_days"] == 7


def test_broken_config_is_a_clear_error(tmp_path, monkeypatch):
    cfg = tmp_path / "config.toml"
    cfg.write_text("[privacy\n")
    monkeypatch.setattr(privacy, "CONFIG_PATH", cfg)
    with pytest.raises(privacy.ConfigError):
        privacy.load()


def test_init_config_never_overwrites(tmp_path, monkeypatch):
    cfg = tmp_path / "stateid" / "config.toml"
    monkeypatch.setattr(privacy, "CONFIG_PATH", cfg)
    assert privacy.init_config() is True
    cfg.write_text("# mine\n")
    assert privacy.init_config() is False
    assert cfg.read_text() == "# mine\n"


# ---------------------------------------------------------------- what agents see of a saved state
def window(title, app_id="brave-browser.desktop", app="Brave", focused=False):
    return {"seq": 1, "title": title, "wm_class": app_id.split(".")[0], "app_id": app_id, "app_name": app,
            "gtk_app_id": None, "pid": 0, "workspace": 0, "monitor": 0,
            "rect": {"x": 0, "y": 0, "width": 800, "height": 600},
            "maximized": False, "fullscreen": False, "minimized": False, "focused": focused, "user_time": 0}


@pytest.fixture
def saved(tmp_path, monkeypatch):
    """A fake saved state on disk: a WhatsApp browser window, a work browser window, a terminal."""
    monkeypatch.setattr(store, "STORE", tmp_path / "states")
    monkeypatch.setattr(privacy, "CONFIG_PATH", tmp_path / "config.toml")
    kid = "DK-TEST0001"
    d = store.STORE / kid
    d.mkdir(parents=True)
    states = [
        {"window": window("WhatsApp - Brave", focused=True), "app": "Brave", "command": [], "cwd": None,
         "files": [], "app_state": {"profile": "Default", "active_tab": 0,
                                    "tabs": [{"url": "https://web.whatsapp.com/", "title": "WhatsApp", "pinned": True}]},
         "ui": None, "screenshot": "window1.png"},
        {"window": window("Docs - Brave"), "app": "Brave", "command": [], "cwd": None, "files": [],
         "app_state": {"profile": "Default", "active_tab": 0, "tabs": [
             {"url": "https://docs.example.com/page?token=SECRET123", "title": "Docs", "pinned": False},
             {"url": "https://mybank.example/account", "title": "Bank account", "pinned": False}]},
         "ui": None, "screenshot": "window2.png"},
        {"window": window("me@host: ~/project", "org.gnome.Ptyxis.desktop", "Terminal"), "app": "Terminal",
         "command": [], "cwd": "/home/me", "files": [],
         "app_state": {"tabs": [{"title": None, "cwd": "/home/me/project", "running": "bash"}],
                       "screen": "$ export GITHUB_TOKEN=ghp_" + "z" * 36 + "\n$ make"},
         "ui": None},
    ]
    (d / "state.json").write_text(json.dumps({"id": kid, "created": "2026-10-26T12:00:00", "scope": "desktop",
                                              "excluded": ["KeePassXC"], "windows": states}))
    return kid


def write_config(text):
    privacy.CONFIG_PATH.write_text(text)


def test_summary_masks_secrets_by_default(saved):
    md = core.summary(saved)
    assert "SECRET123" not in md and "ghp_" not in md
    assert "1 window(s) were not captured" in md
    assert "WhatsApp" in md  # nothing is private until the user says so


def test_private_window_shows_only_the_app(saved):
    write_config('[privacy]\nprivate = ["whatsapp"]\n')
    md = core.summary(saved)
    assert "web.whatsapp.com" not in md and "WhatsApp" not in md
    assert "## 1. Brave — private window" in md
    rows = store.list_states()
    assert rows[0]["windows"][0] == {"app": "Brave", "private": True}


def test_private_tabs_are_hidden_but_others_stay(saved):
    write_config('[privacy]\nprivate_tabs = ["*bank*"]\n')
    md = core.summary(saved)
    assert "mybank" not in md and "Bank account" not in md
    assert "docs.example.com" in md


def test_terminal_text_can_be_turned_off(saved):
    write_config('[privacy]\nterminal_text = false\n')
    assert "export GITHUB_TOKEN" not in core.summary(saved)


def test_private_windows_get_no_screenshots(saved, monkeypatch):
    monkeypatch.setattr(store, "screenshot", lambda kid, name, max_width=1280: b"jpeg")
    assert core.screenshots(saved) == []  # off by default
    write_config('[privacy]\nagent_screenshots = true\n')
    assert [n for n, *_ in core.screenshots(saved)] == [1, 2]
    write_config('[privacy]\nagent_screenshots = true\nprivate = ["whatsapp"]\n')
    assert [n for n, *_ in core.screenshots(saved)] == [2]
    write_config('[privacy]\nagent_screenshots = false\n')
    assert core.screenshots(saved) == []


def test_state_ids_cannot_escape_the_store(saved):
    for bad in ("../../etc/passwd", "DK-TEST0001/../x", "", "DK-123"):
        with pytest.raises(store.StateNotFound):
            store.load(bad)


def test_restore_report_for_agents(saved):
    write_config('[privacy]\nprivate = ["whatsapp"]\n')
    report = {"id": saved, "windows": [
        {"app": "Brave", "title": "WhatsApp - Brave", "status": "launched", "actions": [], "notes": [],
         "launched": ["brave-browser --new-window https://web.whatsapp.com/"]},
        {"app": "Brave", "title": "Docs - Brave", "status": "launched", "actions": [], "notes": [],
         "launched": ["brave-browser --new-window https://docs.example.com/page?token=SECRET123"]},
        {"app": "Terminal", "title": "me@host: ~/project", "status": "already open", "launched": [],
         "actions": [], "notes": ["was running in /home/me: deploy --password=hunter2 (not re-run)"]},
    ]}
    safe = core.agent_safe_report(report)
    text = json.dumps(safe)
    assert safe["windows"][0] == {"app": "Brave", "private": True, "status": "launched"}
    assert "whatsapp" not in text.lower() and "SECRET123" not in text and "hunter2" not in text
    assert safe["windows"][1]["launched"] == ["brave-browser …"]


def test_summary_lists_screenshot_paths_only_when_agents_may_see_them(saved):
    md = core.summary(saved)
    assert "window1.png" not in md and "screenshot" not in md.lower()
    write_config('[privacy]\nagent_screenshots = true\n')
    md = core.summary(saved)
    assert "window1.png" in md and "Each window has a screenshot" in md


def test_summary_puts_the_focused_window_first_and_shows_data_once(saved):
    md = core.summary(saved)
    assert "The user was working in **Brave — WhatsApp - Brave**" in md
    assert md.count("web.whatsapp.com") == 1 and "```json" not in md
    assert "- `/home/me/project` running `bash`" in md
    assert "stateid diff DK-TEST0001" in md
