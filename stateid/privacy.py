"""Privacy settings and secret redaction.

Two levels, set in ~/.config/stateid/config.toml:
  exclude  windows that are never captured: no screenshot, nothing on disk
  private  windows that are captured (so restore still works) but hidden from agents:
           summaries, MCP results and screenshots show only that a private window existed
Text that reaches an agent (terminal screens, commands, URLs, text fields) is passed through
redact(), which masks things that look like passwords, tokens and keys.
"""
import fnmatch
import os
import re
import tomllib
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

CONFIG_PATH = Path(os.environ.get("STATEID_CONFIG", Path.home() / ".config/stateid/config.toml"))

DEFAULTS = {
    "privacy": {
        # never captured
        "exclude": ["keepassxc", "bitwarden", "1password", "seahorse", "passwords and keys",
                    "(incognito)", "(private)", "private browsing", "inprivate"],
        # captured for restore, hidden from agents
        "private": [],
        # browser tabs whose title or URL matches are hidden from agents
        "private_tabs": [],
        "screenshots": True,          # take window screenshots at all
        "agent_screenshots": True,    # let get_state send screenshots to the model
        "terminal_text": True,        # keep the text on terminal screens
        "redact": True,               # mask secrets in everything an agent reads
    },
    "storage": {
        "keep_days": 0,               # delete states older than this on every save; 0 keeps them forever
    },
}

TEMPLATE = """\
# stateid settings. Patterns match, case-insensitively, the app id, window class,
# app name or window title. A plain word matches anywhere; *, ? and [] work as globs.

[privacy]
# Never captured: no screenshot, nothing written to disk.
exclude = ["keepassxc", "bitwarden", "1password", "seahorse", "passwords and keys",
           "(incognito)", "(private)", "private browsing", "inprivate"]

# Captured so `stateid restore` still brings them back, but agents only see
# "a private window was open" (no title, contents or screenshot).
private = []            # e.g. ["whatsapp", "*bank*", "telegram", "mail"]

# Browser tabs (title or URL) hidden from agents; the tab is still restored.
private_tabs = []       # e.g. ["web.whatsapp.com", "*bank*", "mail.google.com"]

screenshots = true        # take window screenshots at all
agent_screenshots = true  # let agents (MCP get_state) receive screenshots
terminal_text = true      # keep the text on terminal screens
redact = true             # mask passwords, tokens and keys in what agents read

[storage]
keep_days = 0           # delete states older than this many days on each save; 0 = never
"""


class ConfigError(ValueError):
    pass


def load():
    cfg = {k: dict(v) for k, v in DEFAULTS.items()}
    try:
        with open(CONFIG_PATH, "rb") as f:
            user = tomllib.load(f)
    except FileNotFoundError:
        return cfg
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise ConfigError(f"Cannot read {CONFIG_PATH}: {e}") from e
    for section, values in user.items():
        if section in cfg and isinstance(values, dict):
            cfg[section].update(values)
    return cfg


def init_config():
    """Write the commented default config; never overwrites an existing one."""
    if CONFIG_PATH.exists():
        return False
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(TEMPLATE)
    return True


def matches(patterns, *fields):
    texts = [f.lower() for f in fields if f]
    for p in patterns or []:
        p = p.lower()
        glob = any(ch in p for ch in "*?[")
        if any(fnmatch.fnmatchcase(t, p) if glob else p in t for t in texts):
            return True
    return False


def window_fields(w):
    return (w.get("app_id"), w.get("wm_class"), w.get("app_name"), w.get("title"))


# ---------------------------------------------------------------- redaction
MASK = "[REDACTED]"
SECRET_PATTERNS = [
    re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?(-----END [A-Z0-9 ]*PRIVATE KEY-----|$)", re.S),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bsk-(?:ant-|proj-)?[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),
    re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),  # JWT
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]{16,}=*"),
]
# key = value / key: value  ->  keep the key, mask the value
KEY_VALUE = re.compile(
    r"(?i)\b((?:[A-Za-z0-9_-]*[_-])?(?:password|passwd|passphrase|pwd|secret|token|api[_-]?key|access[_-]?key"
    r"|private[_-]?key|client[_-]?secret|auth))(\s*[:=]\s*)(\"[^\"]*\"|'[^']*'|[^\s\"',;&]+)")
URL_RE = re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s\"'<>]+", re.I)
SENSITIVE_PARAM = re.compile(r"(?i)token|key|secret|pass|pwd|sig|signature|auth|code|session|sid|credential")


def redact_url(url):
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    netloc = parts.netloc
    if "@" in netloc:  # user:password@host
        netloc = f"{MASK}@" + netloc.rsplit("@", 1)[1]
    query = parts.query
    if query:
        pairs = parse_qsl(query, keep_blank_values=True)
        if any(SENSITIVE_PARAM.search(k) for k, _ in pairs):
            query = urlencode([(k, MASK if SENSITIVE_PARAM.search(k) else v) for k, v in pairs], safe="[]")
    fragment = parts.fragment
    if fragment and ("token" in fragment.lower() or "=" in fragment and SENSITIVE_PARAM.search(fragment)):
        fragment = MASK
    return urlunsplit((parts.scheme, netloc, parts.path, query, fragment))


def redact(text):
    if not text or not isinstance(text, str):
        return text
    text = URL_RE.sub(lambda m: redact_url(m.group(0)), text)
    for rx in SECRET_PATTERNS:
        text = rx.sub(MASK, text)
    return KEY_VALUE.sub(lambda m: m.group(1) + m.group(2) + MASK, text)


def redact_deep(value):
    """Redact every string inside a JSON-like structure."""
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, list):
        return [redact_deep(v) for v in value]
    if isinstance(value, dict):
        return {k: redact_deep(v) for k, v in value.items()}
    return value
