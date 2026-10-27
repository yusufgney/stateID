"""stateid as an MCP server (stdio): lets an AI agent save, read and restore desktop states.

Register with Claude Code:  claude mcp add --scope user stateid -- stateid-mcp
"""
from contextlib import contextmanager
from typing import Literal

from mcp.server.mcpserver import Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from . import __version__, core, privacy, store
from .desktops import DesktopUnavailable

INSTRUCTIONS = """\
stateid captures the user's GNOME desktop as a short state ID such as DK-7SZKSJV0: the open
apps, documents, pages, selected tools (e.g. a red pen in a slideshow), browser tabs, terminal
directories, text cursor positions and window placement, plus a screenshot of every window.

Look at the desktop yourself instead of asking the user to describe or paste it:
- The user refers to something on their screen you have not seen ("this error", "the document
  I have open", "my terminal", "the page I'm on", "where was I?"): call read_desktop (scope
  "focused" first). It reads the windows now, saves nothing and is cheap.
- The user mentions an ID (DK-XXXXXXXX) or asks to continue earlier work: call get_state with
  that ID, or with "latest" when no ID is given, before doing anything else.
- To catch up ("what did I do since DK-…?", "since this morning"), call diff_states; use
  list_states to find the right ID (automatic states, `auto: true`, appear when the user
  turned them on).
- Call save_state only when an ID is useful to keep (the user asks, or before a long task).
- restore_state reopens and rearranges the user's windows: only call it when the user asks.
- The user's privacy settings hide some windows ("private window") and mask secrets as
  [REDACTED]. Respect that: do not try to work around it or guess hidden content.
- Screenshots and terminal text can still contain personal information; do not repeat more
  of it than the task needs."""

mcp = MCPServer("stateid", title="stateid", version=__version__, instructions=INSTRUCTIONS,
                website_url="https://github.com/yusufgney/stateID")

@contextmanager
def expected_errors():
    """Errors the model should see and can act on (unknown ID, extension not loaded, no such window)."""
    try:
        yield
    except (store.StateNotFound, DesktopUnavailable, LookupError, privacy.ConfigError) as e:
        raise ToolError(str(e)) from e


READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False)


@mcp.tool(annotations=READ_ONLY)
def list_windows() -> list[dict]:
    """List the windows open on the user's desktop right now (app, title, focus, workspace).

    Cheap and read-only. `seq` identifies a window for save_state(window_seq=...)."""
    with expected_errors():
        return core.list_windows()


@mcp.tool(annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=False,
                                      open_world_hint=False))
def read_desktop(scope: Literal["focused", "desktop"] = "focused") -> str:
    """What the user is doing right now, read but not saved: for each window its documents, place in
    the app (page, tabs, selected tool), working directory, terminal screen text and text cursor.

    scope: "focused" for the window the user is working in, "desktop" for every window.
    Use it whenever the user refers to something on their screen you have not seen."""
    with expected_errors():
        return core.now(focused=scope == "focused")


@mcp.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False,
                                      open_world_hint=False))
def save_state(scope: Literal["desktop", "focused"] = "desktop", window_seq: int | None = None,
               copy_to_clipboard: bool = False) -> dict:
    """Snapshot the user's windows and return the new state ID with a short summary.

    scope: "desktop" for every window, "focused" for the window the user is working in.
    window_seq: snapshot only this window (from list_windows); overrides scope.
    copy_to_clipboard: also put the ID on the user's clipboard and show a notification.
    Call get_state with the returned ID for the full details and screenshots."""
    with expected_errors():
        return core.save(window=window_seq, focused=scope == "focused" and window_seq is None,
                         copy=copy_to_clipboard)


@mcp.tool(annotations=READ_ONLY)
def diff_states(old_state_id: str, new_state_id: str | None = None) -> str:
    """What changed between two saved states, or since a state if new_state_id is omitted
    (then the desktop is read now, without saving a new state). old_state_id may be "latest".

    Lists windows opened and closed, and for the others the changed title, documents, directory,
    browser tabs, pages and text cursor. Use it to catch up on what the user did since an ID."""
    with expected_errors():
        return core.diff(old_state_id, new_state_id)


@mcp.tool(annotations=READ_ONLY, structured_output=False)
def get_state(state_id: str, include_screenshots: bool = True, max_screenshots: int = 4) -> list:
    """Read a saved state: what was open, where the user was in each app, and window screenshots.

    state_id: an ID such as DK-7SZKSJV0, or "latest" for the newest saved state.
    Returns a Markdown summary (documents, slide/page, selected pen, browser tabs and URLs,
    terminal directories and screen text, text cursor) followed by up to `max_screenshots`
    screenshots, focused window first. Use it to continue the user's work from that moment."""
    with expected_errors():
        out: list = [core.summary(state_id)]
        if include_screenshots:
            for n, app, title, jpeg in core.screenshots(state_id)[:max(0, max_screenshots)]:
                out += [f"Screenshot of window {n}: {app} — {title}", Image(data=jpeg, format="jpeg")]
        return out


@mcp.tool(annotations=READ_ONLY)
def list_states(limit: int = 20) -> list[dict]:
    """List saved states, newest first, with their creation time and windows.

    `auto` is true for states saved automatically (when the user turned that on), false for ones
    the user or an agent saved on purpose."""
    with expected_errors():
        return store.list_states(limit)


@mcp.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True,
                                      open_world_hint=False))
def restore_state(state_id: str) -> dict:
    """Bring a saved moment back on the user's screen: reopen closed apps and documents, put windows
    back in place and re-apply app state (page, pen, slideshow, tabs, cursor). Visible to the user
    and can take up to a minute; only call it when the user asks. Terminal commands are not re-run."""
    with expected_errors():
        return core.agent_safe_report(core.restore(state_id))


@mcp.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=True,
                                      open_world_hint=False))
def delete_state(state_id: str) -> str:
    """Permanently delete a saved state and its screenshots."""
    with expected_errors():
        return f"deleted {store.delete(state_id)}"


@mcp.resource("stateid://states", name="states", mime_type="application/json",
              description="Saved states, newest first")
def states_resource() -> list[dict]:
    return store.list_states(100)


@mcp.resource("stateid://states/{state_id}", name="state", mime_type="text/markdown",
              description="The agent summary (AGENT.md) of one saved state")
def state_resource(state_id: str) -> str:
    return core.summary(state_id)


@mcp.prompt(title="Continue from a state ID")
def continue_from_state(state_id: str = "latest") -> str:
    """Pick up the user's work from a saved desktop state (the newest one by default)."""
    return (f"Call get_state for {state_id}. From the summary and screenshots, work out what I was doing "
            "and where I stopped (documents, pages, tools, terminal output). Summarise it in a few lines, "
            "then continue the task from there. Do not restore windows unless I ask.")


@mcp.prompt(title="Look at my screen")
def look_at_my_screen(question: str = "") -> str:
    """Let the agent read the window the user is working in before answering."""
    ask = f" Then answer: {question}" if question else " Then tell me in a few lines what you see I am doing."
    return ("Call read_desktop with scope \"focused\" (and \"desktop\" if the focused window is not enough) "
            f"to see what I am working on.{ask}")


@mcp.prompt(title="Catch up since a state")
def catch_up(state_id: str = "latest") -> str:
    """Summarise what the user did since a saved state."""
    return (f"Call diff_states with {state_id} to see what changed on my desktop since then. Summarise it "
            "in a few lines: what I opened, closed and changed, and what I seem to be working on now.")


def main():
    mcp.run("stdio")


if __name__ == "__main__":
    main()
