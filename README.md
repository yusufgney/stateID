# stateid

[![CI](https://github.com/yusufgney/stateID/actions/workflows/ci.yml/badge.svg)](https://github.com/yusufgney/stateID/actions/workflows/ci.yml)

**Shared memory of your desktop for you and your AI agents.** stateid gives the current state of your desktop a short ID, like `DK-7SZKSJV0`. An agent can read that ID to see exactly what you were doing and carry on, and you can paste it into a terminal to bring the moment back.

The ID stands for what was open and where you were in it: documents and the page you were on, browser tabs, folders, terminal directories, the text cursor, window placement and a screenshot of every window.

## For AI agents (MCP)

`stateid-mcp` is an [MCP](https://modelcontextprotocol.io) server, so any MCP client (Claude Code, Claude Desktop, Cursor, …) can use your desktop as context. Register it once:

```bash
claude mcp add --scope user stateid -- ~/.local/bin/stateid-mcp
```

Then just talk to the agent:

- *"Save my desktop"*: the agent takes a snapshot and gets an ID.
- *"Continue from DK-7SZKSJV0"*: the agent reads what was open and where you were, then picks up the work.
- *"What did I do since DK-7SZKSJV0?"*: the agent compares that moment with the desktop now.
- *"Why does this fail?"*: the agent reads the window you are working in (nothing is saved).
- *"Continue where I left off"*: with automatic saves on, the agent reads the newest state.
- *"Restore DK-7SZKSJV0"*: the agent reopens and arranges everything.

| Tool | What it does |
|---|---|
| `read_desktop` | What you are doing now (focused window or all), without saving |
| `list_windows` | The windows open right now |
| `get_window_detail` | One window read now, with a live screenshot if you allow them |
| `focus_window` | Bring a window to the front (only when you ask) |
| `open_document` | Open a document or folder in its usual app (only when you ask) |
| `save_state` | Save the programs' work (their own save), snapshot the desktop or one window; returns an ID |
| `check_updates` | Updates waiting for your programs and the risk each means for your saved work |
| `get_state` | A state's summary (`latest` for the newest), plus screenshots if you allow them |
| `diff_states` | What changed since a state, or between two states |
| `list_states` | Saved states, newest first |
| `restore_state` | Bring a state back on screen (only when you ask) |
| `delete_state` | Delete a state |

Without MCP, any agent that can run commands can use `stateid show ID` or `stateid now`, which print the same summary as Markdown.

## Install

Currently for GNOME on Wayland (Ubuntu 26.04 or similar). It needs `python3-gi`, `gir1.2-atspi-2.0` and `gir1.2-gdkpixbuf-2.0`.

```bash
git clone https://github.com/yusufgney/stateID.git ~/stateid
~/stateid/install.sh                 # add --with-freeze for freezable sessions (asks for sudo)
```

Log out and back in once so GNOME Shell loads the extension. `~/stateid/uninstall.sh` removes it.

## Use it yourself

Save with **Super+Shift+S**, from a window's title-bar menu, or with `stateid desktop`. The ID is copied to the clipboard.

```
stateid desktop             save the whole desktop
stateid restore ID          bring the moment back (or just: stateid ID)
stateid diff ID [ID2]       what changed since ID (or between two IDs)
stateid list | show ID | delete ID     (show latest: the newest state)
stateid now [--focused]     the summary of the desktop now, without saving
stateid autosave --enable   save automatically when the windows change (off by default)
stateid run PROGRAM         start a program whose memory is saved too (work it cannot save)
stateid updates             updates waiting for your programs, and whether they are safe for your work
stateid config [--init]     privacy settings
```

## What it captures

- **Any app:** windows, position and workspace, open documents, working directory, text cursor and a screenshot.
- **Deeper app state:** OnlyOffice (slide, pen, slideshow), Chromium browsers (tabs), terminals (directories, resumed Claude Code sessions), Nautilus, Evince and VS Code. More apps, other Linux desktops and Windows 11 are planned.
- **Work in programs that save themselves:** saving a state first presses Ctrl+S in windows with unsaved changes (editors, office suites, image tools) and keeps a copy of their documents in the state; restoring tells you when a document changed since and gives you the copy. Automatic saves never write your files; `--no-save-apps` turns it off. Tried with GNOME Text Editor in a test GNOME session, not yet with other apps or on a real desktop.
- **Work that cannot be saved (experimental):** for programs without a save of their own and for long-running jobs. A program started with `stateid run` has its whole memory frozen with [CRIU](https://criu.org) when you save, so it comes back on restore exactly where it was. Programs that save their own work (LibreOffice, VS Code, GIMP, …) are left to their own saving: `stateid run` refuses them unless you add `--anyway`, and a state tells you when a window had unsaved changes that it does not hold. Programs in these sessions draw without the GPU; `stateid run --gpu` lets OpenGL programs use it through [virgl](https://docs.mesa3d.org/drivers/virgl.html) (tried with Intel and NVIDIA); its relay is built with `cargo` on first use. Its GPU state is saved with the frozen session and rebuilt on restore (tried with an OpenGL demo and GTK 4 apps on NVIDIA and Intel, restoring the same image twice and after a reboot). Vulkan programs draw in software ([lavapipe](https://docs.mesa3d.org/drivers/lavapipe.html)), also with `--gpu`; this was tried with a small Vulkan test program, not yet with real apps.

## Privacy

Agents see only what your settings allow. Screenshots are not sent to agents unless you turn on `agent_screenshots`. In `~/.config/stateid/config.toml` you can exclude apps, hide windows or tabs from agents, and mask secrets such as tokens and passwords. States are stored in your home folder, readable only by you.

## Development

```bash
.venv/bin/python -m pytest
```

CI runs tests, lint, a secret scan and a dependency audit on every push.

## License

MIT
