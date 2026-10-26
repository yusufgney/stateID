# stateid

Give the current state of your desktop a short ID, like `DK-7SZKSJV0`, and bring that exact moment back later.

The ID stands for what was open and where you were in it: the slide you were on, the selected red pen, the browser tabs, the folder, the terminal directories and the line your cursor was on. Paste the ID into a terminal to restore it, or give it to an AI agent so it can read what you were doing and carry on.

```
$ stateid desktop
DK-7SZKSJV0    (copied to the clipboard)

$ stateid DK-7SZKSJV0            # later: reopen and restore everything
• ONLYOFFICE — lecture.pptx — ONLYOFFICE
  already open
  ONLYOFFICE: lecture.pptx: page 30, slideshow at slide 30, pen #E81416
• Text Editor — settings.yml (~/project) - Text Editor
  launched: gnome-text-editor /home/me/project/settings.yml
  Text Editor: cursor at line 6, column 8
```

## Requirements

- GNOME Shell 50 on Wayland (Ubuntu 26.04 or similar)
- `python3-gi`, `gir1.2-atspi-2.0` and `gir1.2-gdkpixbuf-2.0` from your distribution
- `uv` (recommended) or `python3-venv`; the installer creates `.venv` and installs `mcp` and `websockets` into it

## Install

```bash
git clone https://github.com/yusufgney/stateID.git ~/stateid
~/stateid/install.sh
```

Then log out and back in once, so GNOME Shell loads the extension. The installer symlinks the files, so `git pull` updates your installation. To remove it, run `~/stateid/uninstall.sh`. Saved states are kept.

## Use

You can get an ID in three ways:

| Where | How |
|---|---|
| Any window | Right-click the title bar, or **Super + right-click** anywhere inside the window, and choose **Copy state ID of this window** or **Copy state ID of the whole desktop** |
| Keyboard | **Super+Shift+S** saves the whole desktop |
| Terminal | `stateid desktop` |

The ID goes to the clipboard and a notification shows it.

```
stateid desktop             save the whole desktop and copy the ID
stateid save [--window SEQ | --focused] [--copy]   snapshot, print a new ID
stateid restore ID          bring the moment back (shortcut: stateid ID)
stateid show ID             the summary an agent should read (AGENT.md)
stateid list [-n N]         saved states, newest first
stateid delete ID...        delete saved states
stateid config [--init]     show the privacy settings (--init writes a config file)
stateid --help              full help; every command also has --help
```

## Use it from an AI agent (MCP)

`stateid-mcp` is an [MCP](https://modelcontextprotocol.io) server (stdio) with the same features. Register it once, for example with Claude Code:

```bash
claude mcp add --scope user stateid -- ~/.local/bin/stateid-mcp
```

Any MCP client works the same way: the command is `~/.local/bin/stateid-mcp`, with no arguments.

| Tool | What it does | Side effects |
|---|---|---|
| `list_windows` | The windows open right now | read-only |
| `save_state` | Snapshot the desktop, the focused window or one window; returns the ID | writes a new state |
| `get_state` | The summary of a state plus window screenshots (scaled JPEG) | read-only |
| `list_states` | Saved states, newest first | read-only |
| `restore_state` | Bring a state back on screen | opens and moves windows |
| `delete_state` | Delete a state | destructive |

Everything the server returns goes through the privacy settings below.

The server also exposes the resources `stateid://states` and `stateid://states/{id}`, and a prompt, `continue_from_state`.

You can then say *"continue from DK-7SZKSJV0"* or *"save my desktop"* to the agent. Without MCP, ask the agent to run `stateid show DK-7SZKSJV0`. That prints the same Markdown summary, with paths to the screenshots.

## What gets saved

| App | State |
|---|---|
| Every app | Title, position, size, workspace, screenshot, working directory. Open documents, found from the process, the window title and GNOME's recent files. Pressed tool buttons, selected tabs, scroll positions, text fields, and the text cursor (line and column), all read from the accessibility tree. |
| OnlyOffice | Every open document and which one is active. Current slide or page, toolbar tab, selected pen with its colour and size, eraser. A running slideshow with its slide, pen or highlighter, and colour. |
| Brave, Chrome, Chromium | All tabs of the window with their URLs and which one is active. Read from the browser's own session file, so no browser extension is needed. |
| Ptyxis, GNOME Terminal | Each tab's directory, the command running in it, and the text on screen. A Claude Code session open in a tab is resumed with `claude --resume <session id>`. |
| Nautilus | The folder shown. |
| Evince | The document and page. |
| VS Code | The project folder and the active file. |

On restore, `stateid`:

- reopens apps that are closed and reuses windows that are still open;
- puts each window back in its place;
- re-applies the saved state, for example going to the page, selecting the pen, starting the slideshow, or moving the cursor.

Other commands that were running in terminals are printed, not re-run.

## Privacy

Every state is stored in your home folder, readable by you only (`700`/`600`). Settings live in `~/.config/stateid/config.toml`; `stateid config --init` writes a commented starting file and `stateid config` shows what is in effect.

| Setting | Effect |
|---|---|
| `exclude` | Windows that are **never captured**: no screenshot, nothing on disk. The default list covers KeePassXC, Bitwarden, 1Password, Passwords and Keys, and private or incognito browser windows. |
| `private` | Windows that are captured, so `restore` still brings them back, but **hidden from agents**. An agent only learns that a private window of that app was open. |
| `private_tabs` | Browser tabs, matched by title or URL, that are hidden from agents. The tab is still restored. |
| `screenshots` / `agent_screenshots` | Turn screenshots off completely, or keep them on disk but never send them to an agent. |
| `terminal_text` | Whether the text on terminal screens is kept. |
| `redact` | Mask anything that looks like a secret in what agents read: tokens (GitHub, OpenAI and Anthropic, Slack, AWS, Google, JWT), `password=`-style values, private keys, credentials in URLs, and sensitive URL parameters. |
| `keep_days` | Delete states older than this many days on every save; `0` keeps them forever. |

Patterns are plain words, which match anywhere, or globs. They are matched against the app id, the window class, the app name and the window title, for example `private = ["whatsapp", "*bank*"]`.

The agent's view is built from the current settings every time it reads a state. A window you mark private today is also hidden in older states. Relaxing a setting later does not reveal a window that was private when it was saved.

## How it works

- **`extension/`** is a small GNOME Shell extension. It adds the window-menu items and exposes a D-Bus interface at `io.github.yusufgney.StateId`. The interface lists windows, takes screenshots, moves windows and uses the clipboard; on Wayland, only the compositor can do these things.
- **`stateid/`** is the Python package that does the rest:
  - `core.py` holds save, restore and inspect;
  - `cli.py` is the `stateid` command and `mcp_server.py` is `stateid-mcp`, both thin layers over `core.py`;
  - `adapters/` has one module per app, `a11y.py` reads the accessibility tree, and `store.py` handles the saved states;
  - `privacy.py` loads the settings and masks secrets; every summary, listing and report an agent receives passes through it.

  Each app is read in the way that app allows:
  - The **accessibility tree** (AT-SPI) works for any toolkit app.
  - **OnlyOffice** runs its editor in an embedded Chromium. The installer adds `--remote-debugging-port=9333` to its launcher, so `stateid` can ask the editor which slide and pen are active, and set them again.
  - **Chromium browsers** write every tab to an SNSS session file, which `stateid` parses.
  - **Nautilus** reports its open folders over D-Bus.

States are stored in `~/.local/share/stateid/<ID>/` as `state.json`, `AGENT.md` and `windowN.png`. Set `STATEID_DIR` to use another folder.

## Limits

- Anything an app does not expose cannot be restored as data; it stays visible in the screenshot. Examples are the scroll position inside a web page, unsaved form input, and the internal state of Electron apps.
- Unsaved changes are flagged but not recreated. Apps such as Text Editor and OnlyOffice keep their own drafts.
- **Privacy:** `get_state` sends window screenshots and terminal text to the model you use. Redaction is pattern-based and cannot catch everything, for example a password visible in a screenshot. Mark sensitive apps as `private`, or save only the window you want to share with `save_state(scope="focused")`.
- **Security:** the OnlyOffice debug port listens on `127.0.0.1` only, but any program running as your user can use it to control OnlyOffice. Screenshots and terminal text are stored unencrypted in your home folder.

## Development

```bash
uv pip install --python .venv/bin/python -e ".[dev]"
.venv/bin/python -m pytest
```

The tests in `tests/` cover the privacy layer (redaction, matching, what agents see of a saved state) and need no display. Changes to saving and restoring are tested by hand in a separate headless GNOME Shell session (`dbus-run-session -- gnome-shell --headless --wayland --virtual-monitor 1600x900`), so the desktop you are using is never touched.

## License

MIT
