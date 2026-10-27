# stateid

[![CI](https://github.com/yusufgney/stateID/actions/workflows/ci.yml/badge.svg)](https://github.com/yusufgney/stateID/actions/workflows/ci.yml)

Give the current state of your desktop a short ID, like `DK-7SZKSJV0`, and bring that exact moment back later.

The ID stands for what was open and where you were in it: documents and the page you were on, browser tabs, folders, terminal directories, the text cursor and window placement. Paste the ID into a terminal to restore it, or give it to an AI agent so it can see what you were doing and carry on.

```
$ stateid desktop
DK-7SZKSJV0    (copied to the clipboard)

$ stateid DK-7SZKSJV0            # later: reopen and restore everything
```

## Install

Requires GNOME Shell 50 on Wayland (e.g. Ubuntu 26.04) and `python3-gi`, `gir1.2-atspi-2.0`, `gir1.2-gdkpixbuf-2.0`.

```bash
git clone https://github.com/yusufgney/stateID.git ~/stateid
~/stateid/install.sh                 # add --with-freeze for freezable sessions (asks for sudo)
```

Log out and back in once so GNOME Shell loads the extension. `~/stateid/uninstall.sh` removes it.

## Use

Save with **Super+Shift+S**, from a window's title-bar menu, or with `stateid desktop`. The ID is copied to the clipboard.

```
stateid desktop             save the whole desktop
stateid restore ID          bring the moment back (or just: stateid ID)
stateid list | show ID | delete ID
stateid run PROGRAM         start a program whose unsaved work is saved too
stateid config [--init]     privacy settings
```

## What it covers

- **Every app:** windows, their position and workspace, open documents, working directory, text cursor and a screenshot.
- **App-specific state:** OnlyOffice (slide, pen, slideshow), Chromium browsers (tabs), terminals (directories, resumed Claude Code sessions), Nautilus, Evince, VS Code.
- **Unsaved work:** programs started with `stateid run` run in a session whose whole memory is frozen with [CRIU](https://criu.org) when you save, so unsaved changes come back on restore. This uses a small root service installed with `--with-freeze`.
- **AI agents:** `stateid-mcp` is an [MCP](https://modelcontextprotocol.io) server with the same features (`claude mcp add --scope user stateid -- ~/.local/bin/stateid-mcp`).

## Privacy

States are stored in your home folder, readable only by you. In `~/.config/stateid/config.toml` you can exclude apps, hide windows or tabs from agents, turn off screenshots, and mask secrets such as tokens and passwords in what agents read.

## Development

```bash
.venv/bin/python -m pytest
```

CI runs tests, lint, a secret scan and a dependency audit on every push.

## License

MIT
