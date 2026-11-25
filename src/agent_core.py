"""
agent_core.py  --  the brain + tools for the Claude Agent desktop app.

This is a config-driven version of the Google Voice / Claude bot. The GUI
(app.py) builds an AgentConfig from the user's settings, hands it to AgentEngine,
and the engine runs everything on ONE background worker thread:

  * a local chat interface (type in the app, Claude answers),
  * optionally the Google Voice -> Gmail -> SMS bridge (text a phone number),
  * Claude runs as an AGENT with tools:
        - run_cmd / run_powershell            (the computer)
        - read_file / write_file / edit_file / list_directory
        - browser_* (drive the user's Chrome over the DevTools debug port)
        - Gmail / Google Drive connectors     (optional)
        - GitHub connector                    (optional)
        - a custom HTTP connector             (optional)

Everything runs on one thread on purpose: Playwright's sync API is not
thread-safe, so all Claude + browser + Gmail work is serialized here.

Prompt caching is always on (tools+system prefix + growing history).
"""

import base64
import html as html_lib
import json
import os
from datetime import datetime, timedelta
import queue
import re
import shlex
import shutil
import subprocess
import tempfile
import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field
from email.mime.text import MIMEText
from email.utils import parseaddr
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set

# Trust the OS certificate store (fixes CERTIFICATE_VERIFY_FAILED behind
# corporate / content-filter proxies). Must run before any HTTPS client.
try:
    import truststore
    truststore.inject_into_ssl()
    TRUSTSTORE_OK = True
except Exception:
    TRUSTSTORE_OK = False

import requests
import anthropic
from anthropic import Anthropic

# Google libraries are imported lazily inside the methods that need them so the
# app still starts if a user never touches the Google connectors.

# A --windowed exe has no console, so spawning a console child (cmd.exe,
# powershell.exe, node...) makes Windows allocate a fresh black console window
# that flashes up and can steal focus. CREATE_NO_WINDOW suppresses that window
# for the child and everything it spawns. No-op / 0 off Windows.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0

DEFAULT_MODEL = "claude-opus-5"

DEFAULT_SYSTEM = """You are a helpful, capable personal AI assistant running as \
a desktop program on the user's Windows computer. You operate as an AGENT: when \
the user asks you to DO something, use your tools to actually do it, then report \
the result concisely.

You can run cmd and PowerShell, read/write/edit files, drive the user's Chrome \
browser, and (when enabled) use Gmail, Google Drive, and GitHub. Prefer doing \
the task over describing how to do it. If a tool fails, read the error and try a \
sensible alternative before giving up."""

# Yiddish tone block appended when the user writes in Yiddish (the owner here is
# a Heimishe Satmar Yiddish speaker). Kept identical to the other bots.
YIDDISH_BLOCK = """

## When the user writes in Yiddish
Reply in "Heimishe Satmar Yiddish":
- Polite, helpful, intelligent "Heimishe Yid"; conversational and friendly.
- Use ONLY standard Hebrew letters. NEVER use Nekudos, Rafe, or Dagesh.
- Not Daytshmerish. Transliterate technical English terms into Yiddish letters:
  פייל (File), סעטינגס (Settings), עפפ (App), יוזער (User).
- Assume a religious, Orthodox (Satmar/Hasidic) cultural context."""

SMS_BLOCK = """

## Answering an SMS
This particular message arrived as a text to a phone. One Hebrew SMS segment is \
~70 characters, so keep the FINAL reply short - a few lines with the RESULT, not \
a play-by-play. Never paste long command output into the SMS; summarize it."""


# --------------------------------------------------------------------------- #
#  Configuration                                                              #
# --------------------------------------------------------------------------- #

ONE_SHOT_BLOCK = """

## You are ONE-SHOT here - never promise to "check back later"
You exist only while answering THIS message; the moment you give your final \
answer your process ends. You cannot wait for a background job or message the \
user on your own later. So either FINISH the whole job inside this run (waiting \
a few minutes for an install/download is fine), or tell the user to text/type \
"סטאטוס" in a few minutes so you can check how it went on their next message."""


# Generic starter written to data_dir the first time a subscription run looks for
# the playbook. The CLI is told to fill it in; it must stay VALID JSON.
_PLAYBOOK_STARTER = """{
  "_readme": "Long-term memory injected into every subscription run. Match a request to a task by its 'triggers', then follow 'steps'. Keep this VALID JSON (edit with the Edit tool). Add lasting facts and task recipes as you learn them. NEVER store passwords, OTP codes, or API keys.",
  "user": {},
  "environment": {},
  "tasks": [],
  "preferences": [],
  "caveats_global": []
}
"""


# --------------------------------------------------------------------------- #
#  Google Gemini — a second, switchable brain (optional)                       #
# --------------------------------------------------------------------------- #
# Free-tier rate limits are TIGHT and each tool step is its own request, so the
# engine paces itself under the per-minute cap (proactive throttle) and reports
# a used-up DAY instantly instead of blocking. Numbers below are the exact free
# tier caps (AI Studio, 2026-08): flash family = 5 RPM / 20 RPD; flash-lite =
# 15 RPM / 500 RPD; pro = blocked on free (needs billing).
GEMINI_DEFAULT_MODEL = "gemini-3.5-flash-lite"

GEMINI_RPM = {
    "gemini-3.7-flash": 5, "gemini-3.6-flash": 5, "gemini-3.5-flash": 5,
    "gemini-flash-latest": 5, "gemini-3-flash-preview": 5,
    "gemini-3.5-flash-lite": 15, "gemini-3.1-flash-lite": 15,
    "gemini-flash-lite-latest": 15,
    "gemini-3.1-pro-preview": 1, "gemini-pro-latest": 1,
}
GEMINI_RPM_DEFAULT = 5
GEMINI_RPD = {
    "gemini-3.7-flash": 20, "gemini-3.6-flash": 20, "gemini-3.5-flash": 20,
    "gemini-flash-latest": 20, "gemini-3-flash-preview": 20,
    "gemini-3.5-flash-lite": 500, "gemini-3.1-flash-lite": 500,
    "gemini-flash-lite-latest": 500,
}
GEMINI_RPD_DEFAULT = 20
GEMINI_MAX_BLOCK = float(os.getenv("GEMINI_MAX_BLOCK", "70"))
SHARED_CATCHUP_TURNS = int(os.getenv("SHARED_CATCHUP_TURNS", "16"))

_GEMINI_RATE_HINTS = ("429", "resource_exhausted", "quota", "rate limit",
                      "rate_limit", "exceeded")


# Short tone note used when CONTINUING a real terminal session (we must NOT dump
# the agent's full system prompt / playbook into the user's terminal conversation).
_TERMINAL_NOTE = (
    "You are being reached mid-task from outside the terminal. Keep replies SHORT "
    "and, when the user writes Yiddish, answer in Heimishe Satmar Yiddish (Hebrew "
    "letters, no Nekudos). Continue the existing task with your tools, then report "
    "the result briefly — do not restart or re-summarize the whole conversation."
)


_CC_PROJECTS_DIR = Path.home() / ".claude" / "projects"


def _cc_session_file(sid: str, cwd: str = "") -> Optional[Path]:
    """The transcript of session `sid`; with no sid, the newest session in `cwd` -
    the one `claude --continue` resumes."""
    if not _CC_PROJECTS_DIR.is_dir():
        return None
    try:
        if sid:
            hits = list(_CC_PROJECTS_DIR.glob(f"*/{sid}.jsonl"))
            return hits[0] if hits else None
        if cwd:
            # a project folder is named after its cwd, every non-alphanumeric
            # character turned into "-" (C:\Users\You -> C--Users-You)
            d = _CC_PROJECTS_DIR / re.sub(r"[^A-Za-z0-9]", "-", str(cwd))
            files = sorted(d.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
            return files[0] if files else None
    except OSError:
        return None
    return None


def _cc_session_model(path: Optional[Path]) -> str:
    """The model the user's own Claude (desktop app, terminal, claude.ai) last
    answered this session with. Headless runs (entrypoint "sdk-cli", i.e. this
    app or the SMS bot) are skipped so a turn already run on the wrong model
    cannot make it stick. "" when the transcript names none.

    Resuming with the app's own --model used to swap a session started on Opus
    5.5 over to Opus 5 - and the desktop app then stayed there too."""
    if not path:
        return ""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 1048576))
            lines = f.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return ""
    any_model = ""
    for line in reversed(lines):
        if '"assistant"' not in line:
            continue
        try:
            o = json.loads(line)
        except ValueError:
            continue
        if o.get("type") != "assistant":
            continue
        model = str((o.get("message") or {}).get("model") or "")
        if not model.startswith("claude-"):
            continue
        if o.get("entrypoint") != "sdk-cli":
            return model
        any_model = any_model or model
    return any_model


def _cc_last_ai_title(path: Path) -> str:
    """The LATEST `aiTitle` (the friendly session name `claude --resume` shows) from
    a session file. It's rewritten as the title is refined, so read the tail."""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 262144))
            chunk = f.read().decode("utf-8", "replace")
    except OSError:
        return ""
    matches = re.findall(r'"aiTitle":"((?:\\.|[^"\\])*)"', chunk)
    if not matches:
        return ""
    try:
        return json.loads('"' + matches[-1] + '"')
    except ValueError:
        return matches[-1]


def _cc_session_meta(path: Path) -> Optional[dict]:
    """Read a session file -> {entrypoint, cwd, name}, or None if it's not a real
    resumable TERMINAL session. `entrypoint` separates the user's real interactive
    Claude ('cli', 'vscode', ...) from a headless `claude -p` run ('sdk-cli', which
    we do NOT list). `name` = the generated title `claude --resume` shows (the
    latest `aiTitle`), falling back to the first genuine human line."""
    skip = ("<local-command", "<command-", "## Current contents of",
            "[זכרון פון די פריערדיגע")
    entrypoint = cwd = first_msg = ""
    try:
        with open(path, "r", encoding="utf-8") as f:
            for i, line in enumerate(f):
                if i > 60 and first_msg:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    o = json.loads(line)
                except ValueError:
                    continue
                if o.get("type") != "user":
                    continue
                if not entrypoint and o.get("entrypoint"):
                    entrypoint = o.get("entrypoint")
                    cwd = o.get("cwd") or cwd
                if o.get("isMeta"):
                    continue
                c = (o.get("message") or {}).get("content")
                if not first_msg and isinstance(c, str):
                    c = c.strip()
                    if c and not c.startswith(skip):
                        first_msg = re.sub(r"\s+", " ", c)
    except OSError:
        return None
    if not entrypoint or entrypoint == "sdk-cli":
        return None
    name = _cc_last_ai_title(path) or first_msg or "(שמועס אן א נאמען)"
    return {"entrypoint": entrypoint, "cwd": cwd, "name": name}


def _ago_yid(mtime: float) -> str:
    """Short Yiddish 'how long ago' for a session's last activity."""
    mins = max(0, time.time() - mtime) / 60
    if mins < 60:
        return "יעצט" if mins < 2 else f"{int(mins)} מינ'"
    hrs = mins / 60
    if hrs < 24:
        return "1 שעה" if int(hrs) == 1 else f"{int(hrs)} שעה"
    days = hrs / 24
    return "נעכטן" if int(days) == 1 else f"{int(days)} טעג"


def list_terminal_sessions(limit: int = 8) -> List[dict]:
    """His most-recent REAL Claude Code terminal sessions across EVERY project
    folder (so the currently-open session and sessions in any folder show up),
    newest first, each {sid, name, mtime, cwd}. Skips the headless `claude -p`
    (sdk-cli) sessions."""
    if not _CC_PROJECTS_DIR.is_dir():
        return []
    try:
        files = sorted(_CC_PROJECTS_DIR.glob("*/*.jsonl"),
                       key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return []
    out: List[dict] = []
    for p in files[:60]:
        meta = _cc_session_meta(p)
        if not meta:
            continue
        out.append({"sid": p.stem, "name": meta["name"][:55],
                    "mtime": p.stat().st_mtime, "cwd": meta["cwd"]})
        if len(out) >= limit:
            break
    return out


def _is_gemini_rate_error(e) -> bool:
    s = str(e).lower()
    return any(h in s for h in _GEMINI_RATE_HINTS)


def _is_gemini_daily_error(e) -> bool:
    """True if a 429 is the PER-DAY quota (not the per-minute one). Waiting can't
    help a used-up day, so we bail instantly with a clear message instead."""
    if not _is_gemini_rate_error(e):
        return False
    s = str(e).lower()
    return "perday" in s or "per day" in s or "requestsperday" in s or "/day" in s


def _gemini_retry_delay(e, default: float = 8.0) -> float:
    """Pull the server's suggested wait ('Please retry in 16.8s') out of a 429."""
    m = re.search(r"retry\D{0,14}?(\d+(?:\.\d+)?)\s*s", str(e), re.I)
    if m:
        try:
            return min(float(m.group(1)) + 1.0, 62.0)
        except ValueError:
            pass
    return default


# --- Provider / model switch words (text from the phone or chat) -------------
GEMINI_WORDS = {"gemini", "דשעמיני", "דשעמיניי", "דזשעמיני", "דזשעמיניי",
                "גשמיני", "דשמיני", "דעשמיני"}
CLAUDE_WORDS = {"claude", "קלאוד", "קלויד", "קלאד", "קלאווד"}
GEMINI_LIST_WORDS = {"gemini models", "gemini model list", "list gemini",
                     "gemini list", "דשעמיני מאדעלן", "דשעמיניי מאדעלן",
                     "וועלכע דשעמיני מאדעלן", "דשעמיני מאדעל ליסטע"}
GEMINI_ALIASES = {
    "": GEMINI_DEFAULT_MODEL,
    "3.7": "gemini-3.7-flash", "3.6": "gemini-3.6-flash", "3.5": "gemini-3.5-flash",
    "flash": "gemini-3.7-flash", "פלעש": "gemini-3.7-flash",
    "pro": "gemini-3.1-pro-preview",
    "flash lite": "gemini-3.5-flash-lite", "flash-lite": "gemini-3.5-flash-lite",
    "lite": "gemini-3.5-flash-lite",
    "פלעש לייט": "gemini-3.5-flash-lite", "לייט": "gemini-3.5-flash-lite",
    "3.1 pro": "gemini-3.1-pro-preview", "3.1 flash lite": "gemini-3.1-flash-lite",
    # The Gemini 2.5 TEXT models 404 for new keys — point these at live models.
    "2.5 pro": "gemini-3.1-pro-preview", "2.5 flash": "gemini-3.5-flash",
    "2.5 flash lite": "gemini-3.5-flash-lite",
}


def _norm_words(s: str) -> str:
    """Lower-case, collapse whitespace, but KEEP dots/hyphens (they are part of
    version numbers like '3.6' and ids like 'flash-lite')."""
    return re.sub(r"\s+", " ", (s or "").lower().strip())


def _resolve_gemini_model(spec: str) -> str:
    """Turn a shorthand ('', '3.6', '2.5 pro', 'flash') into a full Gemini id."""
    spec = _norm_words(spec)
    if spec in GEMINI_ALIASES:
        return GEMINI_ALIASES[spec]
    if spec.startswith("gemini"):
        return spec.replace(" ", "-")
    parts = spec.split()
    num = parts[0]
    tier = " ".join(parts[1:]) or "flash"
    tier = {"flash lite": "flash-lite", "lite": "flash-lite"}.get(tier, tier).replace(" ", "-")
    return f"gemini-{num}-{tier}"


@dataclass
class AgentConfig:
    anthropic_api_key: str = ""
    model: str = DEFAULT_MODEL

    # --- Google Gemini (a second, switchable brain) -------------------------
    # Optional. When a Gemini API key is set, the user can flip the live brain
    # between Claude and Gemini right from the chat / SMS ("gemini" / "דשעמיני"
    # / "claude"). Gemini runs the SAME tools as Claude (function-calling loop).
    # The free tier is tight (see GEMINI_RPM/GEMINI_RPD below), so the default
    # model is flash-LITE (15/min, 500/day) rather than plain flash (5/min,
    # only 20/day). Get a free key at https://aistudio.google.com/apikey .
    gemini_api_key: str = ""
    gemini_model: str = "gemini-3.5-flash-lite"
    ai_provider: str = "claude"            # startup brain: "claude" | "gemini"

    # --- Which brain? -------------------------------------------------------
    # "api"          -> the Anthropic API (pay per token; needs anthropic_api_key).
    # "claude_code"  -> the Claude Code CLI in headless mode (`claude -p`) running
    #                   on your logged-in Claude SUBSCRIPTION: no API key and no
    #                   per-token cost. The CLI runs as its own agent with its own
    #                   built-in tools (the app's connectors/tools don't apply).
    ai_backend: str = "api"
    claude_code_bin: str = ""              # empty -> auto-detect `claude` on PATH
    claude_code_model: str = ""            # empty -> the CLI's default model
    claude_code_timeout: int = 600
    claude_code_skip_permissions: bool = True
    claude_code_mcp_config: str = ""       # optional MCP servers JSON (e.g. Playwright)
    claude_code_extra_args: str = ""       # extra CLI flags, space separated
    # Auto-compaction (subscription mode): once a per-sender session accumulates
    # this many CLI turns, summarize it and continue in a FRESH session so the
    # transcript (replayed on every --resume) stops growing. 0 disables.
    claude_code_compact_turns: int = 40
    # A small JSON memory file (lasting facts + how-to recipes) injected into every
    # subscription run; the CLI is told to keep it updated. "" -> off.
    playbook_file: str = "bot_memory.json"
    # Where your interactive Claude Code TERMINAL work lives. "terminal"/"המשך"
    # makes the agent resume the most-recent Claude conversation in this folder
    # (`claude -p --continue`), so you can keep going on a task you started at the
    # computer. "" -> the folder next to the app.
    claude_work_dir: str = ""
    # Autonomous: when True, never pause to ask before running a restricted command
    # — the agent just does the task. (Subscription mode already skips the CLI's own
    # prompts via --dangerously-skip-permissions.)
    autonomous: bool = False
    # Opus 5 runs with thinking ON by default; thinking + reply share max_tokens,
    # so give it more headroom than the old thinking-off default of 4096.
    max_tokens: int = 8192
    # How hard the model thinks before it answers: "low" | "medium" | "high" |
    # "xhigh" | "max", or "" for the model's own default. Sent to the API as
    # output_config.effort and to the Claude Code CLI as --effort.
    effort: str = ""
    max_tool_iterations: int = 50
    history_turns: int = 20
    tool_timeout: int = 120
    system_instructions: str = ""          # extra user instructions (appended)

    # File-system / shell access scope
    access_mode: str = "full"              # "full" | "folder"
    allowed_folder: str = ""               # used when access_mode == "folder"

    # Restricted commands: shell commands containing any of these phrases must be
    # approved by the user before they run. One phrase per line (or comma-sep).
    restricted_commands: str = ""

    # Browser
    chrome_debug_port: int = 9222

    # Google (shared OAuth for Gmail + Drive + Google Voice SMS)
    credentials_file: str = ""             # path to OAuth client credentials.json
    gmail_enabled: bool = False
    gdrive_enabled: bool = False

    # Google Voice SMS bridge
    sms_enabled: bool = False
    allowed_numbers: str = ""              # comma separated; empty = allow all
    poll_seconds: int = 10
    sms_chunk_chars: int = 280
    chunk_delay_seconds: float = 3.0
    max_sms_chars: int = 6000
    # How to deliver the SMS reply back to the phone:
    #   "gvoice_web" — drive the already-open Google Voice web page in the debug
    #                  Chrome and send the WHOLE reply as one message (Google
    #                  Voice segments it itself — no "(1/3)" chunking, no GV email
    #                  truncation). Falls back to "gmail" if the browser send fails.
    #   "gmail"      — reply by email; Google Voice turns it into an SMS (needs our
    #                  numbered-chunk splitting).
    reply_via: str = "gvoice_web"
    gvoice_web_timeout: int = 45

    # GitHub connector
    github_enabled: bool = False
    github_token: str = ""

    # Custom HTTP connector
    custom_enabled: bool = False
    custom_name: str = "custom_api"
    custom_base_url: str = ""
    custom_auth_header: str = ""           # e.g. "Authorization: Bearer xxx"
    custom_description: str = ""

    # Auto-update: check a GitHub repo's latest release for a newer version
    update_repo: str = ""                  # e.g. "owner/repo"
    auto_check_updates: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return self.__dict__.copy()

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "AgentConfig":
        known = {k: d[k] for k in cls().__dict__ if k in d}
        return cls(**known)


def digits_only(s: str) -> str:
    return re.sub(r"\D", "", s or "")


def last10(s: str) -> str:
    d = digits_only(s)
    return d[-10:] if len(d) >= 10 else d


def _cap(text: str, limit: int = 6000) -> str:
    text = text or ""
    return text if len(text) <= limit else text[:limit] + "\n...(truncated)"


# --------------------------------------------------------------------------- #
#  Errors the user can understand                                              #
# --------------------------------------------------------------------------- #
# Every reply that reports a failure starts with this mark; the page shows such
# a reply in red with a "Send logs" button beside it.
ERROR_MARK = "⚠️ "
LOG_ROTATE_BYTES = 2_000_000
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")
# What may follow "effort" in a chat or a text message ("" = the model's default).
EFFORT_WORDS = {
    **{lv: lv for lv in EFFORT_LEVELS},
    "x-high": "xhigh", "extra high": "xhigh", "maximum": "max",
    "default": "", "auto": "", "normal": "",
    "נידעריג": "low", "לאו": "low", "מיטל": "medium", "מידיום": "medium",
    "הויך": "high", "הייע": "high", "עקסטרא הויך": "xhigh", "עקס הייע": "xhigh",
    "מאקס": "max", "מאקסימום": "max", "דיפאלט": "", "נארמאל": "",
}
LOG_HINT = "\n\nאויב עס גייט נאך אלץ נישט, דריק \"Send logs\" און מיר וועלן זען וואס צו פארעכטן."
FILTER_EXPLAINED = (
    ERROR_MARK + "דער אינטערנעט פילטער (אדער א פראקסי) האט בלאקירט די פארבינדונג צו "
    "קלאוד. אנשטאט אן ענטפער פון Anthropic איז צוריקגעקומען זיין אייגענער בלאט.\n\n"
    "וואס צו טון:\n"
    "1. שיק א פשוט'ע מעסעדזש, למשל \"שלום\". אויב דאס גייט יא דורך, האט דער פילטער "
    "בלאקירט געוויסע ווערטער אין דיין פריערדיגע מעסעדזש.\n"
    "2. אויב גארנישט גייט נישט דורך, בעט דיין פילטער קאמפאני זיי זאלן עפענען "
    "api.anthropic.com." + LOG_HINT)


class NotAnAnswer(Exception):
    """The API call came back with something that is not a reply from Anthropic:
    a content filter's block page, a proxy's sign-in page, a hotel Wi-Fi portal.
    The SDK does not raise on a non-JSON body - it hands it back as a plain str,
    and the next line (resp.usage) then failed with "'str' object has no
    attribute 'usage'", which told the user nothing."""

    def __init__(self, body: Any):
        self.body = body if isinstance(body, str) else repr(body)
        super().__init__("not an answer from Anthropic: "
                         + _cap(re.sub(r"\s+", " ", self.body), 300))


def _error_chain(e: BaseException) -> List[BaseException]:
    out, seen = [], set()
    while e is not None and id(e) not in seen and len(out) < 6:
        out.append(e)
        seen.add(id(e))
        e = e.__cause__ or e.__context__
    return out


def describe_error(e: BaseException) -> str:
    """One technical line for agent.log: the type, the HTTP status, Anthropic's
    request id and the start of whatever came back - what is needed to tell a
    filter page from a bad key from an outage, later, from the log alone."""
    parts = []
    for x in _error_chain(e):
        p = type(x).__name__
        code = getattr(x, "status_code", None)
        if code:
            p += f" HTTP {code}"
        rid = getattr(x, "request_id", None)
        if rid:
            p += f" request={rid}"
        msg = re.sub(r"\s+", " ", str(x)).strip()
        if msg:
            p += f": {_cap(msg, 300)}"
        body = getattr(x, "body", None)
        if body is not None and not isinstance(x, NotAnAnswer):
            b = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False, default=str)
            b = re.sub(r"\s+", " ", b).strip()
            if b and b not in msg:
                p += f" | body: {_cap(b, 300)}"
        parts.append(p)
    return "  <-  ".join(parts)


def explain_error(e: BaseException) -> str:
    """What went wrong, in plain Yiddish, and what the user can do about it."""
    chain = _error_chain(e)
    text = " ".join(str(x) for x in chain).lower()
    code = next((getattr(x, "status_code", None) for x in chain
                 if getattr(x, "status_code", None)), None)
    body = getattr(e, "body", None)

    if (isinstance(e, (NotAnAnswer, anthropic.APIResponseValidationError))
            or "has no attribute 'usage'" in text or "has no attribute 'content'" in text
            or (isinstance(body, str) and "<html" in body.lower())):
        return FILTER_EXPLAINED
    if isinstance(e, anthropic.AuthenticationError) or code == 401:
        return (ERROR_MARK + "דער Anthropic API קי איז נישט גוט (אדער מען האט אים אויסגעמעקט). "
                "גיי צו Settings, לייג אריין א ריכטיגן קי פון console.anthropic.com, און "
                "דריק Save.")
    if isinstance(e, anthropic.BadRequestError) and "credit balance" in text:
        return (ERROR_MARK + "עס איז נישטא קיין קרעדיט מער אויפן Anthropic אקאונט. "
                "לייג צו קרעדיט אין console.anthropic.com, אונטער Billing, און פרוביר נאכאמאל.")
    if isinstance(e, anthropic.PermissionDeniedError) or code == 403:
        return (ERROR_MARK + "Anthropic לאזט נישט דעם קי טון די זאך (403). טשעק אין "
                "console.anthropic.com אז דער קי איז אקטיוו און אז דער אקאונט איז אין "
                "ארדענונג." + LOG_HINT)
    if isinstance(e, anthropic.NotFoundError) or code == 404:
        return (ERROR_MARK + "דער מאדעל וואס איז אויסגעקליבן איז נישט צו באקומען מיט דיין קי. "
                "קלייב אן אנדערן מאדעל אונטער די מעסעדזש באקס, און פרוביר נאכאמאל.")
    if isinstance(e, anthropic.RateLimitError) or code == 429:
        return (ERROR_MARK + "צו פיל בקשות אין א קורצע צייט; Anthropic בעט צו ווארטן. "
                "ווארט א מינוט און פרוביר נאכאמאל.")
    if isinstance(e, anthropic.RequestTooLargeError) or code == 413:
        return (ERROR_MARK + "די מעסעדזש (אדער א פייל דערין) איז צו גרויס. "
                "פרוביר מיט א קלענערע פייל, אדער מאך א נייע שמועס.")
    if isinstance(e, anthropic.BadRequestError) and ("prompt is too long" in text
                                                     or "too many tokens" in text):
        return (ERROR_MARK + "די שמועס איז געווארן צו לאנג. דריק \"New chat\" און הייב אן פון פריש.")
    if (isinstance(e, (anthropic.InternalServerError, anthropic.OverloadedError))
            or (code or 0) >= 500):
        return (ERROR_MARK + "Anthropic איז יעצט איבערגעלאדן אדער האט א פראבלעם. "
                "ווארט א פאר מינוט און פרוביר נאכאמאל.")
    if isinstance(e, anthropic.APITimeoutError):
        return (ERROR_MARK + "דער ענטפער האט צו לאנג גענומען. דער אינטערנעט איז אפשר פאמעלעך; "
                "פרוביר נאכאמאל." + LOG_HINT)
    if isinstance(e, anthropic.APIConnectionError):
        if "certificate" in text or "ssl" in text:
            return (ERROR_MARK + "דער אינטערנעט פילטער לאזט נישט דורך א זיכערע פארבינדונג צו "
                    "קלאוד (א סערטיפיקאט פראבלעם). בעט דיין פילטער קאמפאני זיי זאלן עפענען "
                    "api.anthropic.com." + LOG_HINT)
        if any(s in text for s in ("getaddrinfo", "name resolution", "11001",
                                   "nodename nor servname", "no address associated")):
            return (ERROR_MARK + "דער קאמפיוטער קען נישט געפינען api.anthropic.com. אדער עס איז "
                    "נישטא קיין אינטערנעט, אדער דער פילטער בלאקירט עס אינגאנצן." + LOG_HINT)
        return (ERROR_MARK + "עס איז נישט געלונגען זיך צו פארבינדן מיט קלאוד. טשעק אז דער "
                "אינטערנעט ארבעט, און פרוביר נאכאמאל." + LOG_HINT)
    if isinstance(e, anthropic.BadRequestError) or code == 400:
        return (ERROR_MARK + "Anthropic האט אפגעזאגט די בקשה (400). מאך א נייע שמועס און "
                "פרוביר נאכאמאל." + LOG_HINT)
    return (ERROR_MARK + f"עפעס איז שיף געגאנגען ({type(e).__name__})." + LOG_HINT)


def _run_tree(cmd, timeout, **kw):
    """subprocess.run, except that a timeout ends the WHOLE process tree.
    subprocess.run kills only the direct child - cmd.exe, or the claude.cmd shim -
    and the real worker under it (node, python, a script) carried on running
    after the call had already reported "timed out". Returns (returncode, timed)."""
    p = subprocess.Popen(cmd, **kw)
    try:
        return p.wait(timeout=timeout), False
    except subprocess.TimeoutExpired:
        if os.name == "nt":
            try:
                subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"],
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, creationflags=_NO_WINDOW,
                               timeout=30)
            except Exception:  # noqa: BLE001
                pass
        try:
            p.kill()
        except OSError:
            pass
        try:
            p.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass
        return None, True


def _origin(url: str):
    from urllib.parse import urlsplit
    u = urlsplit(url.strip())
    return (u.scheme.lower(), u.netloc.lower())


# --------------------------------------------------------------------------- #
#  "Credits used up" detection                                                #
# --------------------------------------------------------------------------- #
# The subscription CLI hits the 5-hour/weekly limit ("You've hit your session
# limit · resets 5:40am") and the API returns a 429 / "credit balance is too low".
# We turn either into a clear Yiddish SMS with the reset time, instead of the
# generic "עפעס איז שיף געגאנגען".
_LIMIT_HINTS = re.compile(
    r"usage limit reached|limit reached|rate[_ -]?limit|too many requests|"
    r"out of credits|credit balance is too low|insufficient (?:credits|quota|balance)|"
    r"quota (?:exceeded|exhausted)|exceeded your .{0,30}limit|upgrade to (?:pro|max)|"
    r"session limit|(?:hit|reached|exceeded) your .{0,30}limit",
    re.I,
)
_LIMIT_EPOCH = re.compile(r"limit reached\s*\|\s*(\d{10,13})", re.I)
_LIMIT_CLOCK = re.compile(
    r"(?:resets?|try again)(?:\s+at)?\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", re.I)
_LIMIT_AFTER = re.compile(r"retry[- _]?after[\"':= ]+(\d+)|try again in (\d+)\s*second", re.I)


def _part_of_day(dt: datetime) -> str:
    h = dt.hour
    if h < 12:
        return "פארמיטאג"
    if h < 17:
        return "נאכמיטאג"
    if h < 21:
        return "אווענט"
    return "ביינאכט"


def _fmt_reset(dt: datetime) -> str:
    now = datetime.now()
    clock = f"{(dt.hour % 12) or 12}:{dt.minute:02d} {_part_of_day(dt)}"
    if dt.date() != now.date():
        clock += f" ({dt.month}/{dt.day})"
    mins = int((dt - now).total_seconds() // 60)
    if mins > 0:
        h, m = divmod(mins, 60)
        rel = (f"{h} שעה " if h else "") + (f"{m} מינוט" if m or not h else "")
        clock += f", אין {rel.strip()}"
    return clock


def _reset_time(text: str) -> Optional[datetime]:
    m = _LIMIT_EPOCH.search(text)
    if m:
        ts = int(m.group(1))
        if ts > 10_000_000_000:
            ts //= 1000
        try:
            return datetime.fromtimestamp(ts)
        except (OverflowError, OSError, ValueError):
            return None
    m = _LIMIT_AFTER.search(text)
    if m:
        return datetime.now() + timedelta(seconds=int(m.group(1) or m.group(2)))
    m = _LIMIT_CLOCK.search(text)
    if m:
        hour, minute, ampm = int(m.group(1)), int(m.group(2) or 0), (m.group(3) or "").lower()
        if ampm == "pm" and hour < 12:
            hour += 12
        elif ampm == "am" and hour == 12:
            hour = 0
        if 0 <= hour <= 23:
            now = datetime.now()
            dt = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if dt <= now:
                dt += timedelta(days=1)
            return dt
    return None


def usage_limit_message(*texts: Any, is_subscription: bool = True) -> Optional[str]:
    """If any of `texts` looks like a credits/limit error, return the Yiddish SMS
    to send back (with the reset time when known). Else None."""
    blob = "\n".join(str(t) for t in texts if t)
    if not blob or not _LIMIT_HINTS.search(blob):
        return None
    where = "די סאבסקריפשן קרעדיטס" if is_subscription else "די API קרעדיטס"
    if re.search(r"credit balance is too low|insufficient (?:credits|balance)", blob, re.I):
        return ("די קרעדיטס האבן זיך אויסגענוצט (דער באלאנס איז ליידיג). "
                "מען דארף צולייגן געלט אין Anthropic Console כדי ווייטער צו גיין.")
    dt = _reset_time(blob)
    if dt:
        return f"{where} האבן זיך אויסגענוצט. עס ווערט ריסעט {_fmt_reset(dt)}."
    return (f"{where} האבן זיך אויסגענוצט. איך ווייס נישט די פונקטליכע צייט פון "
            "ריסעט - געווענליך ווערט עס ריסעט יעדע 5 שעה.")


SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/drive",
]
GMAIL_QUERY = "is:unread from:txt.voice.google.com"


# --------------------------------------------------------------------------- #
#  The engine                                                                 #
# --------------------------------------------------------------------------- #

class AgentEngine:
    """Owns the Claude client, the tool implementations, and the worker thread."""

    def __init__(self, cfg: AgentConfig, data_dir: Path,
                 log_cb: Optional[Callable[[str], None]] = None,
                 chat_cb: Optional[Callable[[str], None]] = None,
                 approval_cb: Optional[Callable[[str], bool]] = None,
                 sms_cb: Optional[Callable[[Dict[str, Any]], None]] = None) -> None:
        self.cfg = cfg
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.token_file = self.data_dir / "token.json"
        self._log_cb = log_cb
        self._chat_cb = chat_cb
        self._sms_cb = sms_cb            # told about every answered text message
        # Called with the command text; must return True to allow it to run.
        self._approval_cb = approval_cb
        self._restricted = [
            p.strip().lower()
            for p in re.split(r"[\n,]", cfg.restricted_commands or "")
            if p.strip()
        ]

        self._use_claude_code = (cfg.ai_backend or "api").strip().lower() in {
            "claude_code", "claudecode", "claude-code", "cli", "code",
            "subscription", "sub",
        }
        # Only build an API client if a key is present (subscription mode needs none).
        self.client = Anthropic(api_key=cfg.anthropic_api_key) if cfg.anthropic_api_key else None
        self.history: Dict[str, List[Dict[str, Any]]] = {}
        self.cc_sessions: Dict[str, str] = {}   # per-sender Claude Code session ids
        self.cc_turns: Dict[str, int] = {}      # accumulated CLI turns per session
        self.cc_mode: Dict[str, str] = {}       # "casual" (default) | "terminal"
        # terminal-session picker: sender -> [session_id,...] we offered (+ parallel
        # cwds), and the chosen session sender -> session_id / its own folder cwd
        # ("" => --continue, the most-recent one).
        self.cc_pick: Dict[str, List[str]] = {}
        self.cc_pick_cwd: Dict[str, List[str]] = {}
        self.cc_resume_session: Dict[str, str] = {}
        self.cc_resume_cwd: Dict[str, str] = {}
        # a model chosen BY TEXT while inside a terminal session, per session id
        self.cc_session_model: Dict[str, str] = {}
        self.seen_ids: Set[str] = set()

        # --- Gemini brain (second, switchable) ------------------------------
        from collections import deque as _deque
        self._deque = _deque
        self._genai = None
        self._genai_types = None
        self._gemini_client = None
        self.gemini_history: Dict[str, List[Any]] = {}   # sender -> [types.Content]
        self.gemini_seeded: Set[str] = set()             # senders given the playbook
        self._gemini_req_times: Dict[str, Any] = {}      # model -> deque[send times]
        self.active_provider = (cfg.ai_provider or "claude").strip().lower()
        self.gemini_model = (cfg.gemini_model or GEMINI_DEFAULT_MODEL).strip()
        # One conversation across model/provider switches (see ask()).
        self.shared_history: Dict[str, List[Dict[str, str]]] = {}
        self.brain_synced: Dict[str, Dict[str, int]] = {}

        self._effort_refused: Set[Any] = set()         # (model, effort) the API said no to
        self._stop = threading.Event()
        self._chat_q: "queue.Queue[str]" = queue.Queue()
        self._thread: Optional[threading.Thread] = None

        # Lazily created, all on the worker thread.
        self._pw = None
        self._browser = None
        self._page = None
        self._gmail = None
        self._drive = None

        self.tools, self.tool_funcs = self._build_tools()
        self.system_blocks = self._build_system()

    # ---- logging -------------------------------------------------------- #
    def log(self, msg: str) -> None:
        line = f"{time.strftime('%H:%M:%S')}  {msg}"
        if self._log_cb:
            try:
                self._log_cb(line)
            except Exception:
                pass
        try:
            path = self.data_dir / "agent.log"
            # Keep it small enough to send: past 2 MB the current log becomes
            # agent.old.log (replacing the one before) and a new one starts.
            if path.exists() and path.stat().st_size > LOG_ROTATE_BYTES:
                os.replace(path, self.data_dir / "agent.old.log")
            with open(path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass

    # ---- system prompt -------------------------------------------------- #
    def _build_system(self) -> List[Dict[str, Any]]:
        text = DEFAULT_SYSTEM
        if self.cfg.access_mode == "folder" and self.cfg.allowed_folder:
            text += (f"\n\n## Restricted workspace\nYou may only read and write "
                     f"files inside this folder: {self.cfg.allowed_folder}\n"
                     f"Do not touch files outside it. Run shell commands from "
                     f"within this folder.")
        if self._restricted:
            text += ("\n\n## Restricted commands\nSome shell commands are "
                     "restricted (phrases: " + ", ".join(self._restricted) + "). "
                     "They need the user's explicit approval, and when nobody can "
                     "approve they are blocked. Do not try to get around a block - "
                     "no scripts, encoded commands or aliases that do the same "
                     "thing. If you need one, say so and stop.")
        text += YIDDISH_BLOCK
        text += SMS_BLOCK
        if self.cfg.system_instructions.strip():
            text += "\n\n## Additional instructions from the user\n" + \
                    self.cfg.system_instructions.strip()
        return [{"type": "text", "text": text,
                 "cache_control": {"type": "ephemeral"}}]

    # ---- sandbox check -------------------------------------------------- #
    def _allowed_path(self, path: str) -> bool:
        if self.cfg.access_mode == "full":
            return True
        if not self.cfg.allowed_folder:
            return False
        root = os.path.realpath(self.cfg.allowed_folder)
        p = os.path.realpath(os.path.abspath(path))
        return p == root or p.startswith(root + os.sep)

    def _cwd(self) -> str:
        if self.cfg.access_mode == "folder" and self.cfg.allowed_folder:
            return self.cfg.allowed_folder
        return str(self.data_dir)

    def _gate(self, command: str) -> Optional[str]:
        """If the command matches a restricted phrase, require user approval.
        Returns None to allow, or a refusal string to block."""
        # Autonomous mode: never pause to ask — just do the task.
        if getattr(self.cfg, "autonomous", False):
            return None
        low = (command or "").lower()
        hit = next((p for p in self._restricted if p and p in low), None)
        if not hit:
            return None
        if not self._approval_cb:
            self.log(f"BLOCKED restricted command (no approver): {command[:80]}")
            return (f"(blocked: this command matches a restricted phrase "
                    f"'{hit}' and needs the user's approval, which isn't "
                    f"available here. Ask the user to run it, or approve it in "
                    f"the app.)")
        self.log(f"Awaiting approval for restricted command: {command[:80]}")
        try:
            approved = bool(self._approval_cb(command))
        except Exception:
            approved = False
        if approved:
            self.log("User APPROVED the command.")
            return None
        self.log("User DENIED the command.")
        return "(the user did not approve running that command)"

    # ================================================================== #
    #  Computer tools                                                     #
    # ================================================================== #
    def _run_shell(self, cmd, *, shell: bool, label: str) -> str:
        """Run a shell command robustly under the frozen/windowed exe.

        Captures output to a temp FILE, not a PIPE. If we used a pipe and the
        command launched a long-lived grandchild (e.g. ``start ... chrome``),
        that grandchild inherits the pipe and Python's reap ``communicate()``
        blocks forever waiting for an EOF that never comes — a --windowed exe
        would freeze silently. A file handle has no such wait. ``stdin=DEVNULL``
        also stops a ``pause``/``set /p`` from hanging (no console stdin).
        """
        to = self.cfg.tool_timeout
        with tempfile.TemporaryFile() as outf:
            try:
                rc, timed = _run_tree(
                    cmd, to, shell=shell, cwd=self._cwd(),
                    stdin=subprocess.DEVNULL, stdout=outf,
                    stderr=subprocess.STDOUT, creationflags=_NO_WINDOW)
            except Exception as e:
                return f"(error: {e})"
            outf.seek(0)
            out = outf.read().decode("utf-8", "replace")
        if timed:
            return _cap(out) + f"\n({label} timed out after {to}s)"
        return _cap(out) or f"(no output, exit code {rc})"

    def t_run_cmd(self, command: str) -> str:
        self.log(f"run_cmd: {command}")
        blocked = self._gate(command)
        if blocked:
            return blocked
        return self._run_shell(command, shell=True, label="command")

    def t_run_powershell(self, script: str) -> str:
        self.log(f"run_powershell: {script[:160]}")
        blocked = self._gate(script)
        if blocked:
            return blocked
        return self._run_shell(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            shell=False, label="script")

    def t_read_file(self, path: str) -> str:
        self.log(f"read_file: {path}")
        if not self._allowed_path(path):
            return "(blocked: that path is outside the allowed folder)"
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                return _cap(f.read())
        except Exception as e:
            return f"(error: {e})"

    def t_write_file(self, path: str, content: str) -> str:
        self.log(f"write_file: {path} ({len(content)} chars)")
        if not self._allowed_path(path):
            return "(blocked: that path is outside the allowed folder)"
        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
            return f"wrote {len(content)} chars to {path}"
        except Exception as e:
            return f"(error: {e})"

    def t_edit_file(self, path: str, old: str, new: str) -> str:
        self.log(f"edit_file: {path}")
        if not self._allowed_path(path):
            return "(blocked: that path is outside the allowed folder)"
        try:
            with open(path, encoding="utf-8") as f:
                data = f.read()
            if old not in data:
                return "(the old text was not found in the file)"
            if data.count(old) > 1:
                return f"(old text appears {data.count(old)} times - make it unique)"
            with open(path, "w", encoding="utf-8") as f:
                f.write(data.replace(old, new, 1))
            return "edit applied"
        except Exception as e:
            return f"(error: {e})"

    def t_list_directory(self, path: str) -> str:
        self.log(f"list_directory: {path}")
        if not self._allowed_path(path):
            return "(blocked: that path is outside the allowed folder)"
        try:
            items = []
            for name in sorted(os.listdir(path)):
                full = os.path.join(path, name)
                items.append(("[DIR] " if os.path.isdir(full) else "[FILE] ") + name)
            return _cap("\n".join(items)) or "(empty)"
        except Exception as e:
            return f"(error: {e})"

    # ================================================================== #
    #  Browser tools (attach to the user's Chrome)                        #
    # ================================================================== #
    def _page_ensure(self):
        from playwright.sync_api import sync_playwright
        if self._pw is None:
            self._pw = sync_playwright().start()
        if self._browser is None or not self._browser.is_connected():
            # 127.0.0.1, not localhost: Chrome binds the debug port to IPv4 only.
            self._browser = self._pw.chromium.connect_over_cdp(
                f"http://127.0.0.1:{self.cfg.chrome_debug_port}")
            self._page = None
        if self._page is None or self._page.is_closed():
            ctx = (self._browser.contexts[0] if self._browser.contexts
                   else self._browser.new_context())
            self._page = ctx.pages[0] if ctx.pages else ctx.new_page()
        return self._page

    def _browser_err(self, e: Exception) -> str:
        return (f"(browser error: {e}) - make sure Chrome is running with the "
                f"debug port open (use the 'Open Chrome for the AI' button).")

    def t_browser_navigate(self, url: str) -> str:
        self.log(f"browser_navigate: {url}")
        try:
            page = self._page_ensure()
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            return f"opened {page.url}\ntitle: {page.title()}"
        except Exception as e:
            return self._browser_err(e)

    def t_browser_read(self) -> str:
        self.log("browser_read")
        try:
            page = self._page_ensure()
            text = page.evaluate("() => document.body ? document.body.innerText : ''")
            return f"url: {page.url}\ntitle: {page.title()}\n\n{_cap(text)}"
        except Exception as e:
            return self._browser_err(e)

    def t_browser_click(self, selector: str) -> str:
        self.log(f"browser_click: {selector}")
        try:
            page = self._page_ensure()
            try:
                page.click(selector, timeout=8000)
            except Exception:
                page.get_by_text(selector, exact=False).first.click(timeout=8000)
            return f"clicked {selector} -> now at {page.url}"
        except Exception as e:
            return self._browser_err(e)

    def t_browser_fill(self, selector: str, text: str) -> str:
        self.log(f"browser_fill: {selector}")
        try:
            page = self._page_ensure()
            page.fill(selector, text, timeout=8000)
            return f"filled {selector}"
        except Exception as e:
            return self._browser_err(e)

    def t_browser_js(self, script: str) -> str:
        self.log(f"browser_js: {script[:160]}")
        try:
            page = self._page_ensure()
            return _cap(str(page.evaluate(script)))
        except Exception as e:
            return self._browser_err(e)

    def t_browser_screenshot(self):
        self.log("browser_screenshot")
        try:
            page = self._page_ensure()
            png = page.screenshot(type="png", full_page=False)
            return {"type": "image", "source": {
                "type": "base64", "media_type": "image/png",
                "data": base64.b64encode(png).decode()}}
        except Exception as e:
            return self._browser_err(e)

    # ================================================================== #
    #  Google connectors (Gmail + Drive) -- share one OAuth token         #
    # ================================================================== #
    def _google_creds(self, interactive: bool = False):
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        from google.auth.exceptions import RefreshError
        from google_auth_oauthlib.flow import InstalledAppFlow

        creds = None
        if self.token_file.exists():
            creds = Credentials.from_authorized_user_file(str(self.token_file), SCOPES)
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                try:
                    creds.refresh(Request())
                except RefreshError:
                    creds = None
            if not creds or not creds.valid:
                if not self.cfg.credentials_file or not os.path.exists(self.cfg.credentials_file):
                    raise RuntimeError("No Google credentials.json set. Add it in Setup, "
                                       "then click 'Connect Google Account'.")
                # The consent flow opens a browser and BLOCKS until someone clicks
                # through it - run_local_server has no timeout. From the worker
                # thread that froze chat and SMS for good while the UI still said
                # "Running". Only the explicit button may start it.
                if not interactive:
                    raise RuntimeError(
                        "Google needs you to sign in again. Open Setup and click "
                        "'Connect Google Account'.")
                flow = InstalledAppFlow.from_client_secrets_file(
                    self.cfg.credentials_file, SCOPES)
                creds = flow.run_local_server(port=0, timeout_seconds=300)
            self.token_file.write_text(creds.to_json())
        return creds

    def connect_google_interactive(self) -> str:
        """Called from the GUI's 'Connect Google Account' button. Runs the OAuth
        browser flow and stores token.json. Safe to call on any thread."""
        # A token that is present but lacks a scope still counts as "valid", so
        # without this the button could never fix it. Remove it for real.
        try:
            self.token_file.unlink()
        except OSError:
            pass
        creds = self._google_creds(interactive=True)
        try:
            from googleapiclient.discovery import build
            svc = build("gmail", "v1", credentials=creds)
            prof = svc.users().getProfile(userId="me").execute()
            return f"Connected as {prof.get('emailAddress', '(unknown)')}"
        except Exception as e:
            return f"Token saved, but verification failed: {e}"

    def _gmail_service(self):
        if self._gmail is None:
            from googleapiclient.discovery import build
            self._gmail = build("gmail", "v1", credentials=self._google_creds())
        return self._gmail

    def _drive_service(self):
        if self._drive is None:
            from googleapiclient.discovery import build
            self._drive = build("drive", "v3", credentials=self._google_creds())
        return self._drive

    # ---- Gmail tools ---------------------------------------------------- #
    def t_gmail_search(self, query: str) -> str:
        self.log(f"gmail_search: {query}")
        try:
            svc = self._gmail_service()
            res = svc.users().messages().list(userId="me", q=query, maxResults=10).execute()
            out = []
            for m in res.get("messages", []) or []:
                full = svc.users().messages().get(userId="me", id=m["id"],
                                                  format="metadata",
                                                  metadataHeaders=["From", "Subject", "Date"]).execute()
                hdrs = {h["name"]: h["value"] for h in full["payload"]["headers"]}
                out.append(f"[{m['id']}] {hdrs.get('Date','')} | {hdrs.get('From','')} | {hdrs.get('Subject','')}")
            return _cap("\n".join(out)) or "(no matches)"
        except Exception as e:
            return f"(gmail error: {e})"

    def t_gmail_read(self, message_id: str) -> str:
        self.log(f"gmail_read: {message_id}")
        try:
            svc = self._gmail_service()
            full = svc.users().messages().get(userId="me", id=message_id, format="full").execute()
            body, _, _ = get_message_text(full["payload"])
            return _cap(body) or "(empty)"
        except Exception as e:
            return f"(gmail error: {e})"

    def t_gmail_send(self, to: str, subject: str, body: str) -> str:
        self.log(f"gmail_send: to={to} subj={subject[:60]}")
        try:
            svc = self._gmail_service()
            mime = MIMEText(body, "plain", "utf-8")
            mime["To"] = to
            mime["Subject"] = subject
            raw = base64.urlsafe_b64encode(mime.as_bytes()).decode()
            svc.users().messages().send(userId="me", body={"raw": raw}).execute()
            return f"sent to {to}"
        except Exception as e:
            return f"(gmail error: {e})"

    # ---- Drive tools ---------------------------------------------------- #
    def t_gdrive_search(self, query: str) -> str:
        self.log(f"gdrive_search: {query}")
        try:
            svc = self._drive_service()
            q = f"name contains '{query}' and trashed = false"
            res = svc.files().list(q=q, pageSize=20,
                                   fields="files(id,name,mimeType,modifiedTime)").execute()
            out = [f"[{f['id']}] {f['name']} ({f['mimeType']})"
                   for f in res.get("files", [])]
            return _cap("\n".join(out)) or "(no matches)"
        except Exception as e:
            return f"(drive error: {e})"

    def t_gdrive_download(self, file_id: str, dest_path: str) -> str:
        self.log(f"gdrive_download: {file_id} -> {dest_path}")
        if not self._allowed_path(dest_path):
            return "(blocked: destination is outside the allowed folder)"
        try:
            from googleapiclient.http import MediaIoBaseDownload
            import io
            svc = self._drive_service()
            meta = svc.files().get(fileId=file_id, fields="name,mimeType").execute()
            request = svc.files().get_media(fileId=file_id)
            os.makedirs(os.path.dirname(dest_path) or ".", exist_ok=True)
            with io.FileIO(dest_path, "wb") as fh:
                downloader = MediaIoBaseDownload(fh, request)
                done = False
                while not done:
                    _, done = downloader.next_chunk()
            return f"downloaded '{meta.get('name')}' to {dest_path}"
        except Exception as e:
            return f"(drive error: {e})"

    def t_gdrive_upload(self, path: str, name: str = "") -> str:
        self.log(f"gdrive_upload: {path}")
        if not self._allowed_path(path):
            return "(blocked: source path is outside the allowed folder)"
        try:
            from googleapiclient.http import MediaFileUpload
            svc = self._drive_service()
            meta = {"name": name or os.path.basename(path)}
            media = MediaFileUpload(path, resumable=True)
            f = svc.files().create(body=meta, media_body=media, fields="id,name").execute()
            return f"uploaded as '{f['name']}' (id {f['id']})"
        except Exception as e:
            return f"(drive error: {e})"

    # ================================================================== #
    #  GitHub connector                                                   #
    # ================================================================== #
    def _gh_headers(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self.cfg.github_token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28"}

    def t_github_list(self, repo: str, path: str = "") -> str:
        self.log(f"github_list: {repo}/{path}")
        try:
            url = f"https://api.github.com/repos/{repo}/contents/{path}"
            r = requests.get(url, headers=self._gh_headers(), timeout=30)
            if r.status_code >= 400:
                return f"(github {r.status_code}: {r.text[:200]})"
            data = r.json()
            if isinstance(data, dict):
                return f"{data.get('type')}: {data.get('name')}"
            return _cap("\n".join(f"{i['type']}: {i['path']}" for i in data)) or "(empty)"
        except Exception as e:
            return f"(github error: {e})"

    def t_github_read(self, repo: str, path: str) -> str:
        self.log(f"github_read: {repo}/{path}")
        try:
            url = f"https://api.github.com/repos/{repo}/contents/{path}"
            r = requests.get(url, headers=self._gh_headers(), timeout=30)
            if r.status_code >= 400:
                return f"(github {r.status_code}: {r.text[:200]})"
            data = r.json()
            content = base64.b64decode(data.get("content", "")).decode("utf-8", "replace")
            return _cap(content)
        except Exception as e:
            return f"(github error: {e})"

    def t_github_write(self, repo: str, path: str, content: str, message: str) -> str:
        self.log(f"github_write: {repo}/{path}")
        try:
            url = f"https://api.github.com/repos/{repo}/contents/{path}"
            sha = None
            g = requests.get(url, headers=self._gh_headers(), timeout=30)
            if g.status_code == 200:
                sha = g.json().get("sha")
            body = {"message": message,
                    "content": base64.b64encode(content.encode()).decode()}
            if sha:
                body["sha"] = sha
            r = requests.put(url, headers=self._gh_headers(), json=body, timeout=30)
            if r.status_code >= 400:
                return f"(github {r.status_code}: {r.text[:200]})"
            return f"committed {path} to {repo}"
        except Exception as e:
            return f"(github error: {e})"

    # ================================================================== #
    #  Custom HTTP connector                                              #
    # ================================================================== #
    def t_custom_api(self, method: str, path: str, body: str = "") -> str:
        self.log(f"custom_api: {method} {path}")
        try:
            base = self.cfg.custom_base_url.rstrip("/")
            url = path if path.startswith("http") else base + "/" + path.lstrip("/")
            # The auth header is a secret meant for ONE site. A full URL in `path`
            # used to send it anywhere - one instruction hidden in an e-mail or a
            # web page was enough to hand the key to a stranger's server.
            if _origin(url) != _origin(base):
                return (f"(custom connector: refused - this tool may only call "
                        f"{self.cfg.custom_base_url})")
            headers = {}
            secret = ":" in self.cfg.custom_auth_header
            if secret:
                k, v = self.cfg.custom_auth_header.split(":", 1)
                headers[k.strip()] = v.strip()
            data = None
            if body:
                headers.setdefault("Content-Type", "application/json")
                data = body.encode()
            # requests drops "Authorization" on a cross-site redirect but keeps
            # any other header name (X-API-Key, ...), so with a secret header
            # set, redirects are reported instead of followed.
            r = requests.request(method.upper(), url, headers=headers, data=data,
                                 timeout=45, allow_redirects=not secret)
            if r.is_redirect:
                return f"HTTP {r.status_code} (redirect to {r.headers.get('Location', '?')})"
            return _cap(f"HTTP {r.status_code}\n{r.text}")
        except Exception as e:
            return f"(custom connector error: {e})"

    # ================================================================== #
    #  Tool registry                                                      #
    # ================================================================== #
    def _build_tools(self):
        tools: List[Dict[str, Any]] = []
        funcs: Dict[str, Callable] = {}

        def add(spec, fn):
            tools.append(spec)
            funcs[spec["name"]] = fn

        obj = lambda props, req=None: {"type": "object", "properties": props,
                                       **({"required": req} if req else {})}

        # --- computer ---
        add({"name": "run_cmd", "description": "Run a Windows cmd.exe command and return its output.",
             "input_schema": obj({"command": {"type": "string"}}, ["command"])}, self.t_run_cmd)
        add({"name": "run_powershell", "description": "Run a PowerShell script and return its output.",
             "input_schema": obj({"script": {"type": "string"}}, ["script"])}, self.t_run_powershell)
        add({"name": "read_file", "description": "Read the full text of a file (absolute path).",
             "input_schema": obj({"path": {"type": "string"}}, ["path"])}, self.t_read_file)
        add({"name": "write_file", "description": "Create or overwrite a file (absolute path).",
             "input_schema": obj({"path": {"type": "string"}, "content": {"type": "string"}},
                                 ["path", "content"])}, self.t_write_file)
        add({"name": "edit_file", "description": "Replace one unique occurrence of old text with new text in a file.",
             "input_schema": obj({"path": {"type": "string"}, "old": {"type": "string"},
                                  "new": {"type": "string"}}, ["path", "old", "new"])}, self.t_edit_file)
        add({"name": "list_directory", "description": "List files and folders in a directory (absolute path).",
             "input_schema": obj({"path": {"type": "string"}}, ["path"])}, self.t_list_directory)

        # --- browser ---
        add({"name": "browser_navigate", "description": "Open a URL in the user's Chrome; returns the title.",
             "input_schema": obj({"url": {"type": "string"}}, ["url"])}, self.t_browser_navigate)
        add({"name": "browser_read", "description": "Return the visible text of the current Chrome page.",
             "input_schema": obj({})}, self.t_browser_read)
        add({"name": "browser_click", "description": "Click an element by CSS selector or visible text.",
             "input_schema": obj({"selector": {"type": "string"}}, ["selector"])}, self.t_browser_click)
        add({"name": "browser_fill", "description": "Type text into an input/textarea by CSS selector.",
             "input_schema": obj({"selector": {"type": "string"}, "text": {"type": "string"}},
                                 ["selector", "text"])}, self.t_browser_fill)
        add({"name": "browser_js", "description": "Run a JavaScript expression on the current page and return the result.",
             "input_schema": obj({"script": {"type": "string"}}, ["script"])}, self.t_browser_js)
        add({"name": "browser_screenshot", "description": "Take a screenshot of the current Chrome page so you can see it.",
             "input_schema": obj({})}, self.t_browser_screenshot)

        # --- Gmail ---
        if self.cfg.gmail_enabled:
            add({"name": "gmail_search", "description": "Search the user's Gmail. Returns id | date | from | subject.",
                 "input_schema": obj({"query": {"type": "string"}}, ["query"])}, self.t_gmail_search)
            add({"name": "gmail_read", "description": "Read the body text of a Gmail message by id.",
                 "input_schema": obj({"message_id": {"type": "string"}}, ["message_id"])}, self.t_gmail_read)
            add({"name": "gmail_send", "description": "Send an email from the user's Gmail.",
                 "input_schema": obj({"to": {"type": "string"}, "subject": {"type": "string"},
                                      "body": {"type": "string"}}, ["to", "subject", "body"])}, self.t_gmail_send)

        # --- Drive ---
        if self.cfg.gdrive_enabled:
            add({"name": "gdrive_search", "description": "Search Google Drive by file name. Returns id, name, type.",
                 "input_schema": obj({"query": {"type": "string"}}, ["query"])}, self.t_gdrive_search)
            add({"name": "gdrive_download", "description": "Download a Drive file by id to a local path.",
                 "input_schema": obj({"file_id": {"type": "string"}, "dest_path": {"type": "string"}},
                                     ["file_id", "dest_path"])}, self.t_gdrive_download)
            add({"name": "gdrive_upload", "description": "Upload a local file to Google Drive.",
                 "input_schema": obj({"path": {"type": "string"}, "name": {"type": "string"}},
                                     ["path"])}, self.t_gdrive_upload)

        # --- GitHub ---
        if self.cfg.github_enabled:
            add({"name": "github_list", "description": "List a path in a GitHub repo (owner/repo, path).",
                 "input_schema": obj({"repo": {"type": "string"}, "path": {"type": "string"}},
                                     ["repo"])}, self.t_github_list)
            add({"name": "github_read", "description": "Read a file from a GitHub repo (owner/repo, path).",
                 "input_schema": obj({"repo": {"type": "string"}, "path": {"type": "string"}},
                                     ["repo", "path"])}, self.t_github_read)
            add({"name": "github_write", "description": "Create/update a file in a GitHub repo with a commit.",
                 "input_schema": obj({"repo": {"type": "string"}, "path": {"type": "string"},
                                      "content": {"type": "string"}, "message": {"type": "string"}},
                                     ["repo", "path", "content", "message"])}, self.t_github_write)

        # --- custom ---
        if self.cfg.custom_enabled and self.cfg.custom_base_url:
            desc = self.cfg.custom_description or "Call the configured custom HTTP API."
            add({"name": self.cfg.custom_name or "custom_api",
                 "description": f"{desc} (base URL {self.cfg.custom_base_url})",
                 "input_schema": obj({"method": {"type": "string"}, "path": {"type": "string"},
                                      "body": {"type": "string"}}, ["method", "path"])}, self.t_custom_api)

        return tools, funcs

    def run_tool(self, name: str, args: Dict[str, Any]):
        fn = self.tool_funcs.get(name)
        if not fn:
            return f"(unknown tool {name})"
        try:
            return fn(**(args or {}))
        except TypeError as e:
            return f"(bad arguments: {e})"
        except Exception as e:
            return f"(tool error: {e})"

    # ================================================================== #
    #  The Claude agent loop (with prompt caching)                        #
    # ================================================================== #
    # ---- control commands (chat + SMS) -------------------------------- #
    _MODEL_ALIASES = {
        "opus5": "claude-opus-5", "opus": "claude-opus-5",
        "opus48": "claude-opus-4-8", "opus47": "claude-opus-4-7",
        "fable": "claude-fable-5", "fable5": "claude-fable-5",
        "sonnet": "claude-sonnet-5", "sonnet5": "claude-sonnet-5",
        "haiku": "claude-haiku-4-5", "haiku45": "claude-haiku-4-5",
        # Yiddish spellings (bare + version, dots stripped: "אפוס 4.8" -> "אפוס48")
        "אפוס": "claude-opus-5", "אפוס5": "claude-opus-5",
        "אפוס48": "claude-opus-4-8", "אפוס47": "claude-opus-4-7",
        "פעיבל": "claude-fable-5", "פעיבל5": "claude-fable-5",
        "סאנעט": "claude-sonnet-5", "סאנעט5": "claude-sonnet-5",
        "הייקו": "claude-haiku-4-5", "הייקו45": "claude-haiku-4-5",
    }

    def _resolve_model(self, name: str) -> Optional[str]:
        n = re.sub(r"[\s._\-]+", "", (name or "").lower())
        return self._MODEL_ALIASES.get(n)

    def handle_command(self, sender: str, text: str) -> Optional[str]:
        """If `text` is a control word, act on it and return a reply. Otherwise
        return None so the caller treats it as a normal question. Works for both
        the local chat and Google Voice SMS."""
        t = (text or "").strip()
        low = t.lower()

        if low in ("reset", "clear", "נייע שמועס", "נייע שמועה", "נײע שמועס"):
            self._forget(sender)
            return "די שמועס איז אויסגעמעקט - מיר הייבן אן פון פריש (טעמפּ)."

        # After we sent the terminal-session list, a bare number picks that row and
        # jumps back into it (like choosing a row in `claude --resume`).
        if sender in self.cc_pick:
            m = re.match(r"^\s*(?:נומער\s*)?#?(\d{1,2})\s*[.)]?\s*$", t)
            if m:
                idx = int(m.group(1)) - 1
                sids = self.cc_pick.get(sender, [])
                cwds = self.cc_pick_cwd.get(sender, [])
                if 0 <= idx < len(sids):
                    sid = sids[idx]
                    cwd = cwds[idx] if idx < len(cwds) else ""
                    name = next((s["name"] for s in list_terminal_sessions()
                                 if s["sid"] == sid), "")
                    self.cc_pick.pop(sender, None)
                    self.cc_pick_cwd.pop(sender, None)
                    self.cc_resume_session[sender] = sid
                    self.cc_resume_cwd[sender] = cwd
                    self.cc_mode[sender] = "terminal"
                    self.active_provider = "claude"
                    folder = Path(cwd).name if cwd else "?"
                    return (f"🔵 צוריק אין שמועס #{idx + 1} ({folder}): {name}\n"
                            "שיק דײַן מעסעדזש און איך גיי ווײַטער פֿון דארט - אין דײַן "
                            "עכטע טערמינאל-שמועס. ('טעמפּ' = צוריק צו א סתם שמועס)")
                return (f"נומער {idx + 1} איז נישטא אין די ליסטע. ענטפער מיט א נומער "
                        "פֿון די ליסטע, אָדער 'טעמפּ' פֿאַר א נײַע שמועס.")
            self.cc_pick.pop(sender, None)   # not a number - let the message flow on
            self.cc_pick_cwd.pop(sender, None)

        # Show the terminal-session picker (like `claude --resume`): his REAL
        # interactive Claude sessions across every folder, newest first.
        if low in ("terminal", "continue", "טערמינאל", "המשך", "ממשיך", "פארזעץ",
                   "resume", "רעזום"):
            if not self._use_claude_code:
                return "טערמינאל-שמועס אַרבעט נאָר אין Claude סאָבסקריפּשאָן מאָוד."
            self.active_provider = "claude"
            sessions = list_terminal_sessions()
            if not sessions:
                return ("קיין טערמינאל-שמועסן געפֿונען. שיק 'טעמפּ' פֿאַר א נײַע שמועס.")
            self.cc_pick[sender] = [s["sid"] for s in sessions]
            self.cc_pick_cwd[sender] = [s["cwd"] for s in sessions]
            lines = ["וועלכע טערמינאל-שמועס ווילסטו צוריקגיין? ענטפער מיט א נומער:"]
            for i, s in enumerate(sessions, 1):
                folder = Path(s["cwd"]).name if s["cwd"] else "?"
                lines.append(f"{i}. [{_ago_yid(s['mtime'])} · {folder}] {s['name']}")
            lines.append("שיק 'טעמפּ' פֿאַר א סתם נײַע שמועס.")
            return "\n".join(lines)
        if low in ("temp", "casual", "טעמפ", "טעמפּ", "סתם שמועס", "קאזשועל"):
            self.cc_mode[sender] = "casual"
            self.cc_pick.pop(sender, None)
            self.cc_pick_cwd.pop(sender, None)
            self.cc_resume_session.pop(sender, None)
            self.cc_resume_cwd.pop(sender, None)
            return "אריבער צו א טעמפּ (סתם) שמועס - נישט פֿאַרבונדן מיט די טערמינאל-אַרבעט."

        has_gemini = bool((self.cfg.gemini_api_key or "").strip())

        if low in ("help", "הילף", "commands", "קאמאנדס", "קאָמאַנדס"):
            lines = ["קאמאנדס:",
                     "נייע שמועס / reset - הייב אן א פרישע שמועס",
                     "טערמינאל / continue - ליסטע פֿון דײַנע טערמינאל-שמועסן (ענטפער מיט א נומער)",
                     "טעמפּ / temp - צוריק צו א סתם שמועס",
                     "model opus 5 / fable / sonnet / haiku / opus 4.8 - טויש מאדעל"]
            if has_gemini:
                lines += ["gemini / דשעמיני - אריבער צו Gemini",
                          "gemini 3.6 / דשעמיני פלעש לייט - א ספעציפישע Gemini מאדעל",
                          "claude / אפוס 4.8 - צוריק צו Claude",
                          "דשעמיני מאדעלן - ליסט אלע Gemini מאדעלן"]
            lines += ["effort low / medium / high / xhigh / max / default - וויפיל דער מאדעל זאל טראכטן",
                      "וועלכע מאדעל - זאג וואס לויפט יעצט", "help - די ליסטע"]
            return "\n".join(lines)

        # "effort high" / "עפארט הויך": how hard the model thinks, from the next
        # message on. A bare "effort" says what it is now.
        for prefix in ("effort", "עפארט", "עפפארט", "עפאָרט"):
            if low == prefix or low.startswith(prefix + " "):
                word = low[len(prefix):].strip()
                if not word:
                    return f"דער עפארט איז איצט: {self.cfg.effort or 'דער מאדעל׳ס דיפאלט'}"
                level = EFFORT_WORDS.get(word)
                if level is None:
                    return "איך קען נישט יענעם עפארט. פרואוו: low, medium, high, xhigh, max, default."
                self.cfg.effort = level
                return f"פון יעצט: עפארט {level or 'דיפאלט (דער מאדעל באשטימט)'}."

        # "gemini models" / "דשעמיני מאדעלן" -> list models (check BEFORE the
        # switch, else it would try to switch to a model literally named "models").
        if has_gemini and _norm_words(t) in GEMINI_LIST_WORDS:
            return self.list_gemini_models()

        # A bare provider/model word flips the active brain WITHOUT wiping the
        # chat: each brain keeps its own session and ask() catches the newly-active
        # brain up on anything said while it was away.
        switch = self.parse_provider_switch(t)
        if switch:
            provider, model_id = switch
            if provider == "gemini" and not has_gemini:
                return ("Gemini איז נישט איינגעשטעלט - לייגט אריין א Gemini API קי "
                        "אינעם סעטאפ טאב.")
            self.active_provider = provider
            if provider == "gemini":
                self.gemini_model = model_id
                return f"אריבער צו Gemini: {model_id}. (די זעלבע שמועס גייט אן.)"
            if self._use_claude_code and self.cc_mode.get(sender) == "terminal":
                return self._set_terminal_model(sender, model_id)
            if self._use_claude_code:
                self.cfg.claude_code_model = model_id or self.cfg.claude_code_model
            elif model_id:
                self.cfg.model = model_id
            shown = model_id or "ברירת-המחדל"
            return f"אריבער צו Claude: {shown}. (די זעלבע שמועס גייט אן.)"

        if low in ("which model", "model?", "וועלכע מאדעל", "וועלכן מאדעל",
                   "וואס פאר א מאדעל", "מאדעל?"):
            return f"דער מאדעל איז איצט: {self.current_model(sender)}"

        for prefix in ("model", "מאדעל", "מאָדעל", "מאדל"):
            if low.startswith(prefix):
                name = t[len(prefix):].strip()
                if not name:
                    return f"דער מאדעל איז איצט: {self.current_model(sender)}"
                model_id = self._resolve_model(name)
                if not model_id:
                    return ("איך קען נישט יענעם מאדעל. פרואוו: "
                            "opus 5, fable, sonnet, haiku, opus 4.8")
                self.active_provider = "claude"   # explicit Claude model comes back
                if self._use_claude_code and self.cc_mode.get(sender) == "terminal":
                    return self._set_terminal_model(sender, model_id)
                if self._use_claude_code:
                    self.cfg.claude_code_model = model_id
                else:
                    self.cfg.model = model_id
                return (f"די מאדעל איז געטוישט צו {model_id}. "
                        "(די זעלבע שמועס גייט אן.)")

        return None

    def ask(self, sender: str, question: str) -> str:
        """Dispatch to whichever brain is active (Claude API / Claude Code CLI /
        Gemini), keeping ONE conversation across model/provider switches. Each
        brain keeps its own native session; when the active brain has missed
        turns (said in the OTHER brain) we prepend just those to the question so
        a switch feels like one continuous chat."""
        brain = self.active_provider
        hist = self.shared_history.setdefault(sender, [])
        synced = self.brain_synced.setdefault(sender, {})

        catchup = hist[synced.get(brain, 0):]
        if len(catchup) > SHARED_CATCHUP_TURNS:
            catchup = catchup[-SHARED_CATCHUP_TURNS:]
        q = question
        if catchup:
            lines = []
            for turn in catchup:
                who = "יוזער" if turn["role"] == "user" else "דו (דער אסיסטענט)"
                lines.append(f"{who}: {turn['text']}")
            q = ("[די שמועס ביז אהער - ניץ עס ווי קאנטעקסט, מיר גייען אן אין די "
                 "זעלבע שמועס]\n" + "\n".join(lines) + "\n\n---\n\n" + question)

        hist.append({"role": "user", "text": question})
        if brain == "gemini":
            reply = self._ask_gemini(sender, q)
        elif self._use_claude_code:
            reply = self.ask_claude_code(sender, q)
        else:
            reply = self._ask_claude_api(sender, q)
        hist.append({"role": "assistant", "text": reply})
        synced[brain] = len(hist)
        return reply

    def _create_message(self, history):
        """One Messages API call, with the effort from Settings when one is set.
        Not every model takes every effort level (Haiku 4.5 takes none, the 4.6
        models have no "xhigh"); a model that refuses it is asked again without
        it, and is remembered so it is not refused every turn."""
        model = self.cfg.model
        effort = (self.cfg.effort or "").strip().lower()
        kw = dict(model=model, max_tokens=self.cfg.max_tokens, system=self.system_blocks,
                  tools=self.tools, messages=history, cache_control={"type": "ephemeral"})
        if effort not in EFFORT_LEVELS or (model, effort) in self._effort_refused:
            return self.client.messages.create(**kw)
        if effort in ("high", "xhigh", "max"):
            # More thinking shares the same max_tokens with the answer.
            kw["max_tokens"] = max(self.cfg.max_tokens, 16000)
        try:
            return self.client.messages.create(output_config={"effort": effort}, **kw)
        except anthropic.BadRequestError as e:
            if "effort" not in str(e).lower() and "output_config" not in str(e).lower():
                raise
            self._effort_refused.add((model, effort))
            self.log(f"Effort '{effort}' is not supported by {model}; using its default. "
                     f"({describe_error(e)})")
            kw["max_tokens"] = self.cfg.max_tokens
            return self.client.messages.create(**kw)

    def _ask_claude_api(self, sender: str, question: str) -> str:
        if self.client is None:
            return "אנטשולדיג - עס פעלט אן Anthropic API קי (סעטאפ טאב)."
        history = self.history.setdefault(sender, [])
        history.append({"role": "user", "content": question})
        max_msgs = self.cfg.history_turns * 2
        if len(history) > max_msgs:
            del history[: len(history) - max_msgs]
            # A cut can land between a tool_use and its tool_result; the API then
            # rejects the whole request (400) and the context was wiped. Start
            # the window at a plain user question instead.
            while history and not (history[0]["role"] == "user"
                                   and isinstance(history[0]["content"], str)):
                history.pop(0)

        for _ in range(self.cfg.max_tool_iterations):
            if self._stop.is_set():
                return "(אפגעשטעלט)"
            try:
                resp = self._create_message(history)
                if not (hasattr(resp, "usage") and hasattr(resp, "content")):
                    raise NotAnAnswer(resp)
            except Exception as e:
                self.log(f"Claude API error ({self.cfg.model}): {describe_error(e)}")
                self.history.pop(sender, None)
                return explain_error(e)

            u = resp.usage
            self.log(f"usage in={u.input_tokens} "
                     f"cache_read={getattr(u,'cache_read_input_tokens',0)} "
                     f"cache_write={getattr(u,'cache_creation_input_tokens',0)} "
                     f"out={u.output_tokens}")

            history.append({"role": "assistant", "content": resp.content})

            if resp.stop_reason == "tool_use":
                results = []
                for block in resp.content:
                    if block.type == "tool_use":
                        out = self.run_tool(block.name, block.input)
                        content = out if isinstance(out, list) else \
                            [out] if isinstance(out, dict) else out
                        results.append({"type": "tool_result",
                                        "tool_use_id": block.id,
                                        "content": content})
                history.append({"role": "user", "content": results})
                continue

            text = "".join(b.text for b in resp.content if b.type == "text").strip()
            reply = (text or "(no answer)").replace("*", "").strip()
            reply = re.sub(r"\n{3,}", "\n\n", reply)
            return reply

        return "(צו פיל שריט - איך האב זיך אפגעשטעלט)"

    # ================================================================== #
    #  Claude Code CLI backend (runs on your subscription, no API cost)   #
    # ================================================================== #
    # Instead of paying the Anthropic API per token, shell out to `claude -p`
    # (headless Claude Code). Because the CLI is logged in with the user's Claude
    # subscription it runs on the subscription — no API key, no per-token cost.
    # The CLI runs as its OWN agent with its OWN built-in tools, so the app's
    # tool registry / connectors do not apply in this mode. Per-sender continuity
    # uses Claude Code sessions (--session-id to start, --resume to continue).
    def _claude_code_bin(self) -> str:
        return (self.cfg.claude_code_bin.strip()
                or shutil.which("claude") or "claude")

    def _claude_child_env(self) -> Dict[str, str]:
        """Env for the `claude` child. Strip ANTHROPIC_API_KEY / AUTH_TOKEN so the
        CLI authenticates with the logged-in SUBSCRIPTION, not a key (which would
        silently bill the API)."""
        env = os.environ.copy()
        for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
            env.pop(k, None)
        # Never let our own `claude -p` call trigger a self-update: it replaces
        # claude.exe in place, and a spawn during that window fails ("not found").
        env["DISABLE_AUTOUPDATER"] = "1"
        return env

    def _cli_system_prompt(self) -> str:
        # Reuse the built system prompt (persona + Yiddish + SMS + the user's own
        # rules), plus the one-shot reminder (each `claude -p` dies after answering).
        return self.system_blocks[0]["text"] + ONE_SHOT_BLOCK

    def _playbook_path(self) -> Optional[Path]:
        name = (self.cfg.playbook_file or "").strip()
        return (self.data_dir / name) if name else None

    def _read_playbook(self) -> str:
        p = self._playbook_path()
        if not p:
            return ""
        if not p.exists():
            try:                                 # seed a generic starter once
                p.write_text(_PLAYBOOK_STARTER, encoding="utf-8")
            except OSError:
                return ""
        try:
            return p.read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    def _terminal_model(self, resume_session_id: str, continue_dir) -> str:
        """--model for resuming a terminal session: one chosen by text for this
        session, else the session's own model, else the configured one."""
        f = _cc_session_file(resume_session_id, continue_dir or "")
        key = resume_session_id or (f.stem if f else "")
        return (self.cc_session_model.get(key) or _cc_session_model(f)
                or self.cfg.claude_code_model.strip())

    def _terminal_key(self, sender: str) -> str:
        sid = self.cc_resume_session.get(sender, "")
        if sid:
            return sid
        f = _cc_session_file("", self.cc_resume_cwd.get(sender) or
                             self.cfg.claude_work_dir or "")
        return f.stem if f else ""

    def _set_terminal_model(self, sender: str, model_id: str) -> str:
        """'model X' while inside one of the user's own sessions changes THAT
        session only; the configured default stays as it was."""
        key = self._terminal_key(sender)
        if not key:
            return "איך געפין נישט דעם טערמינאל-שמועס. שיק 'טערמינאל' פאר די ליסטע."
        if model_id:
            self.cc_session_model[key] = model_id
        else:
            self.cc_session_model.pop(key, None)
        shown = model_id or _cc_session_model(_cc_session_file(key)) or "ברירת-המחדל"
        return f"די מאדעל פאר דעם שמועס איז יעצט {shown}. (די זעלבע שמועס גייט אן.)"

    def _run_claude_cli(self, prompt: str, session_id: str, resume: bool,
                        continue_dir=None, resume_session_id: str = ""):
        """One `claude -p` call. Returns (text, ok, raw_error, turns). If continue_dir
        is set, resume a Claude Code TERMINAL conversation in THAT folder with only a
        short SMS note (no playbook): resume_session_id -> `--resume <id>` (the exact
        session the user picked), else `--continue` (the most-recent one)."""
        terminal = bool(continue_dir)
        cc_bin = self._claude_code_bin()
        via_cmd = os.name == "nt" and cc_bin.lower().endswith((".cmd", ".bat"))

        sys_prompt = self._cli_system_prompt()
        mcp = self.cfg.claude_code_mcp_config.strip()
        mcp_on = bool(mcp) and Path(mcp).exists()
        if mcp_on:
            sys_prompt += (
                "\n\n## Browser tools are available\n"
                "You CAN drive the user's already-open Chrome via the Playwright "
                "browser tools (browser_navigate, browser_click, browser_type, "
                "browser_take_screenshot, ...). Use them for anything on the web.")

        # A SHORT pointer goes in the system prompt (which becomes a command-line
        # argument); the full JSON is injected via stdin on the first turn (below).
        # Passing the whole playbook on the command line trips Windows' command-line
        # length limit -> WinError 206 "filename or extension too long", which Python
        # surfaces as FileNotFoundError (see the bot's 2026-08-11 regression).
        pb_path = self._playbook_path()
        playbook_dump = ""
        if pb_path:
            note = self._read_playbook()
            sys_prompt += (
                f"\n\n## Your saved memory ({pb_path.name})\n"
                f"There is a JSON memory file at `{pb_path.name}` in your working "
                "folder with lasting facts about the user and step-by-step recipes "
                "for recurring tasks (matched by Yiddish trigger phrases). Its current "
                "contents are given to you at the start of this session; READ a task's "
                "steps (or re-open the file with your Read tool) and follow them instead "
                "of re-figuring it out. Keep it current with the Edit tool (keep it "
                "VALID JSON) when you learn the working steps for a task or a lasting "
                "fact - NEVER store passwords, OTP codes, or API keys.")
            playbook_dump = (f"## Current contents of {pb_path.name}\n"
                             + (note if note else "(empty so far)"))

        if terminal:
            # Resuming the user's real terminal conversation — keep our footprint tiny.
            sys_prompt = _TERMINAL_NOTE
            playbook_dump = ""

        args = ["-p", "--output-format", "json"]
        # The user's own session keeps its own model; only this app's scratch chat
        # runs on the configured one.
        model = (self._terminal_model(resume_session_id, continue_dir) if terminal
                 else self.cfg.claude_code_model.strip())
        if model:
            args += ["--model", model]
        # The effort chosen in Settings - but not on a resumed terminal session,
        # which keeps the effort it was started with (same rule as its model).
        if self.cfg.effort in EFFORT_LEVELS and not terminal:
            args += ["--effort", self.cfg.effort]
        if self.cfg.claude_code_skip_permissions:
            args.append("--dangerously-skip-permissions")
        # The headless CLI has no way to stop and ask, so a "needs approval"
        # command used to simply RUN. Block those prefixes outright instead.
        # (Prefix rules cannot see a phrase buried mid-command - the API backend's
        # _gate is the stricter of the two.)
        if self._restricted and not getattr(self.cfg, "autonomous", False):
            rules = []
            for ph in self._restricted:
                ph = (ph or "").strip()
                if ph and " " not in ph:
                    rules += [f"Bash({ph}:*)", f"PowerShell({ph}:*)"]
            if rules:
                args += ["--disallowedTools", ",".join(rules)]
        if mcp_on:
            args += ["--mcp-config", mcp]
        if terminal:
            args += ["--resume", resume_session_id] if resume_session_id else ["--continue"]
        else:
            args += ["--resume", session_id] if resume else ["--session-id", session_id]
        if not via_cmd:
            args += ["--append-system-prompt", sys_prompt]
        args += shlex.split(self.cfg.claude_code_extra_args or "")

        argv = (["cmd", "/c", cc_bin] if via_cmd else [cc_bin]) + args
        # Build stdin (no length limit). On the FIRST turn prepend the full playbook
        # JSON so the model sees it without a tool call; on --resume it's already in
        # the replayed transcript. The cmd-shim path can't use --append-system-prompt,
        # so it also prepends sys_prompt here.
        preamble = ""
        if via_cmd and not resume:
            preamble += sys_prompt + "\n\n---\n\n"
        if playbook_dump and not resume:
            preamble += playbook_dump + "\n\n---\n\n"
        stdin_text = preamble + prompt

        to = self.cfg.claude_code_timeout
        t0 = time.monotonic()
        with tempfile.TemporaryFile() as inf, tempfile.TemporaryFile() as outf, \
                tempfile.TemporaryFile() as errf:
            inf.write(stdin_text.encode("utf-8")); inf.seek(0)
            try:
                rc, timed = _run_tree(
                    argv, to, cwd=(continue_dir if terminal else self._cwd()),
                    env=self._claude_child_env(),
                    stdin=inf, stdout=outf, stderr=errf,
                    creationflags=_NO_WINDOW)
            except OSError as e:
                # winerror 206 ("filename or extension too long" = command line too
                # long) is mapped by Python to errno 2 -> FileNotFoundError, so log
                # the real codes instead of blaming a missing binary.
                self.log(f"claude spawn failed (bin={cc_bin!r} "
                         f"winerror={getattr(e, 'winerror', None)} "
                         f"errno={getattr(e, 'errno', None)}): {e}")
                return "(claude CLI נישט געפונען - אינסטאלירט Claude Code)", False, str(e), 0
            except Exception as e:
                self.log(f"claude -p failed: {e}")
                return f"(error: {e})", False, str(e), 0
            outf.seek(0); out = outf.read().decode("utf-8", "replace")
            errf.seek(0); err = errf.read().decode("utf-8", "replace")
        dur = time.monotonic() - t0

        if timed:
            self.log(f"claude -p timed out after {to}s")
            return "(claude האט צו לאנג גענומען)", False, "", 0
        try:
            data = json.loads(out.strip()) if out.strip() else None
        except ValueError:
            data = None
        if isinstance(data, dict):
            turns = int(data.get("num_turns") or 0)
            if data.get("is_error") or data.get("subtype") not in (None, "success"):
                raw = f"{data.get('result') or ''}\n{data.get('subtype') or ''}\n{err}"
                self.log("claude -p error: " + _cap(str(data.get("result") or err), 300))
                return str(data.get("result") or "(claude error)"), False, raw, turns
            self.log(f"claude -p ok in {dur:.1f}s (turns={data.get('num_turns')})")
            return str(data.get("result", "")).strip(), True, "", turns
        txt = out.strip() or err.strip()
        if rc not in (0, None) and not out.strip():
            self.log(f"claude -p exit={rc}: " + _cap(err, 300))
            return txt or f"(claude error rc={rc})", False, f"{out}\n{err}", 0
        return txt, True, "", 0

    def _summarize_session(self, old_sid: str) -> str:
        """Fold the current (big) session into a short handoff note for a fresh one."""
        prompt = (
            "Summarize our whole conversation so far into a SHORT handoff note for a "
            "fresh session that keeps helping this same user: who they are and their "
            "preferences, what they are working on, any unfinished task and how to "
            "continue it, and facts worth remembering. Just the note - no preamble.")
        text, ok, _raw, _turns = self._run_claude_cli(prompt, old_sid, resume=True)
        return text.strip() if ok and text.strip() else ""

    def _maybe_compact(self, sender: str, sid: str, question: str):
        """If this session has grown big, summarize it and start fresh. Returns
        (sid, resume, question)."""
        budget = self.cfg.claude_code_compact_turns
        if not (budget and self.cc_turns.get(sender, 0) >= budget):
            return sid, True, question
        summary = self._summarize_session(sid)
        if not summary:
            return sid, True, question           # summary failed - keep old session
        self.log(f"Compacted {sender} after {self.cc_turns.get(sender, 0)} turns "
                 "into a fresh session.")
        self.cc_turns[sender] = 0
        seeded = ("[זכרון פון די פריערדיגע שמועס - ניץ עס ווי קאנטעקסט]\n"
                  + summary + "\n\n---\n\n" + question)
        return str(uuid.uuid4()), False, seeded

    def _work_dir(self) -> str:
        return (self.cfg.claude_work_dir or "").strip() or str(self.data_dir)

    def ask_claude_code(self, sender: str, question: str) -> str:
        """Answer via the Claude Code CLI (subscription). In "terminal" mode, continue
        the user's real terminal conversation; otherwise keep a per-sender session."""
        if self.cc_mode.get(sender) == "terminal":
            # resume the picked session in ITS OWN folder (sessions are per-cwd);
            # fall back to the configured work dir + --continue if none was chosen.
            wd = self.cc_resume_cwd.get(sender) or self._work_dir()
            if not Path(wd).is_dir():
                return (f"דער טערמינאל-פֿאָלדער איז נישטא: {wd}. שיק 'טערמינאל' פֿאַר א "
                        "פֿרישע ליסטע, אָדער 'טעמפּ' פֿאַר א נײַע שמועס.")
            reply, ok, raw, _turns = self._run_claude_cli(
                question, "", resume=False, continue_dir=wd,
                resume_session_id=self.cc_resume_session.get(sender, ""))
            if not ok:
                lim = usage_limit_message(reply, raw)
                if lim:
                    return lim
                self.log("terminal-continue failed: " + _cap(raw or reply, 200))
                return ("נישט געקענט ממשיך זײַן די טערמינאל-שמועס (אפֿשר איז נאָך נישטא "
                        "קיין שמועס אין יענעם פֿאָלדער). שיק 'טעמפּ' פֿאַר א נײַע שמועס.")
            reply = (reply or "(קיין ענטפער)").replace("*", "").strip()
            return re.sub(r"\n{3,}", "\n\n", reply)

        sid = self.cc_sessions.get(sender)
        resume = sid is not None
        if resume:
            sid, resume, question = self._maybe_compact(sender, sid, question)
        if not sid:
            sid = str(uuid.uuid4())

        reply, ok, raw, turns = self._run_claude_cli(question, sid, resume)
        limit = None if ok else usage_limit_message(reply, raw)
        if not ok and not limit and resume:
            self.log(f"claude -p resume failed for {sender}; starting fresh session.")
            sid = str(uuid.uuid4())
            self.cc_turns[sender] = 0
            reply, ok, raw, turns = self._run_claude_cli(question, sid, resume=False)
            limit = None if ok else usage_limit_message(reply, raw)

        if not ok:
            if limit:
                self.log("Usage/credit limit hit (Claude Code): " + _cap(raw or reply, 200))
                self.cc_sessions[sender] = sid   # still good once credits return
                return limit
            self.cc_sessions.pop(sender, None)
            self.cc_turns.pop(sender, None)
            return reply or "אנטשולדיג, עפעס איז שיף געגאנגען. פרוביר נאכאמאל."

        self.cc_sessions[sender] = sid
        self.cc_turns[sender] = self.cc_turns.get(sender, 0) + max(turns, 1)
        reply = (reply or "(קיין ענטפער)").replace("*", "").strip()
        return re.sub(r"\n{3,}", "\n\n", reply)

    # ================================================================== #
    #  Gemini backend (second brain — same tools, function-calling loop)  #
    # ================================================================== #
    def _import_genai(self):
        if self._genai is None:
            from google import genai as _g
            from google.genai import types as _t
            self._genai, self._genai_types = _g, _t
        return self._genai, self._genai_types

    def _gemini_client_get(self):
        if self._gemini_client is None:
            key = (self.cfg.gemini_api_key or "").strip()
            if not key:
                raise RuntimeError("no Gemini API key (set it in the Setup tab)")
            genai, _ = self._import_genai()
            self._gemini_client = genai.Client(api_key=key)
        return self._gemini_client

    def _js_to_gemini_schema(self, js: Dict[str, Any]):
        """Convert one Anthropic/JSON-schema dict into a google.genai types.Schema."""
        _, types = self._import_genai()
        t = (js.get("type") or "string").upper()
        if t == "OBJECT":
            props = {k: self._js_to_gemini_schema(v)
                     for k, v in (js.get("properties") or {}).items()}
            return types.Schema(type="OBJECT", properties=props or None,
                                required=js.get("required") or None)
        if t == "ARRAY":
            items = js.get("items") or {"type": "string"}
            return types.Schema(type="ARRAY", items=self._js_to_gemini_schema(items))
        return types.Schema(type=t)

    def _gemini_tool_decls(self):
        """Turn the shared Anthropic tool list into Gemini FunctionDeclarations."""
        _, types = self._import_genai()
        decls = []
        for spec in self.tools:
            schema = spec.get("input_schema") or {}
            props = schema.get("properties") or {}
            params = self._js_to_gemini_schema(schema) if props else None
            decls.append(types.FunctionDeclaration(
                name=spec["name"], description=spec.get("description", ""),
                parameters=params))
        return [types.Tool(function_declarations=decls)]

    def _gemini_system_instruction(self) -> str:
        """The shared persona/tone/tool rules, plus the same memory pointer the
        CLI uses (so both brains share one set of facts)."""
        s = self.system_blocks[0]["text"]
        pb = self._playbook_path()
        if pb:
            s += (f"\n\n## Your saved memory ({pb.name})\n"
                  f"A JSON memory file at `{pb.name}` in your working folder holds "
                  "lasting facts about the user and step-by-step recipes for recurring "
                  "tasks. Its current contents are given to you at the start of this "
                  "conversation; FOLLOW a task's saved steps instead of re-figuring it "
                  "out. Keep it current with your read_file/write_file/edit_file tools "
                  "(keep it VALID JSON) — NEVER store passwords, OTP codes, or API keys.")
        return s

    def _gemini_seed_prefix(self) -> str:
        """The full playbook JSON, prepended to a new session's first message."""
        pb = self._playbook_path()
        note = self._read_playbook() if pb else ""
        if pb and note:
            return f"## Current contents of {pb.name}\n{note}\n\n---\n\n"
        return ""

    def _gemini_throttle(self, model: str) -> None:
        """Block until sending one more request to `model` stays within its
        per-minute budget (60s sliding window per model). Keeps us under the free
        tier RPM so we never 429 in normal use."""
        limit = GEMINI_RPM.get(model, GEMINI_RPM_DEFAULT)
        dq = self._gemini_req_times.setdefault(model, self._deque())
        now = time.monotonic()
        while dq and now - dq[0] >= 60.0:
            dq.popleft()
        if len(dq) >= limit:
            wait = 60.0 - (now - dq[0]) + 0.3
            if wait > 0:
                self.log(f"Gemini throttle ({model}): {limit}/min reached, "
                         f"waiting {wait:.1f}s")
                time.sleep(min(wait, GEMINI_MAX_BLOCK))
            now = time.monotonic()
            while dq and now - dq[0] >= 60.0:
                dq.popleft()
        dq.append(time.monotonic())

    def _gemini_rate_message(self) -> str:
        return (f"Gemini ({self.gemini_model}) איז יעצט בוזי - די פרייע קוואטע איז "
                "איבערגעשטיגן. פרוביר אין א מינוט, אדער נעם 'דשעמיני 3.6' / 'קלאוד', "
                "אדער שטעל אריין בילינג ביי Google.")

    def _gemini_daily_message(self) -> str:
        rpd = GEMINI_RPD.get(self.gemini_model, GEMINI_RPD_DEFAULT)
        return (f"Gemini ({self.gemini_model}) האט אויסגענוצט די טעגליכע פרייע קוואטע "
                f"({rpd} ריקוועסטס אין א טאג). נעם 'דשעמיני פלעש לייט' (500 א טאג) "
                "אדער 'קלאוד', אדער שטעל אריין בילינג ביי Google. עס נייט זיך אויף "
                "ביי האלבע נאכט (Pacific צייט).")

    def _ask_gemini(self, sender: str, question: str) -> str:
        """Answer via Gemini, keeping per-sender history and running the same tools."""
        _, types = self._import_genai()
        try:
            client = self._gemini_client_get()
        except Exception as e:
            self.log(f"Gemini unavailable: {e}")
            return "Gemini איז נישט גרייט - עס פעלט אן API קי (סעטאפ טאב)."

        config = types.GenerateContentConfig(
            system_instruction=self._gemini_system_instruction(),
            tools=self._gemini_tool_decls(),
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )

        def _generate(contents):
            """One generate_content call: paces itself under the per-minute quota,
            and if a 429 still slips through WAITS OUT the minute (bounded by
            GEMINI_MAX_BLOCK) rather than erroring back at the user. A used-up DAY
            is re-raised so the caller can give an instant 'switch model' message."""
            deadline = time.monotonic() + GEMINI_MAX_BLOCK
            delay = 6.0
            while True:
                self._gemini_throttle(self.gemini_model)
                try:
                    return client.models.generate_content(
                        model=self.gemini_model, contents=contents, config=config)
                except Exception as e:
                    if _is_gemini_daily_error(e):
                        raise
                    if _is_gemini_rate_error(e) and time.monotonic() < deadline:
                        wait = _gemini_retry_delay(e, delay)
                        wait = min(wait, max(1.0, deadline - time.monotonic()))
                        self.log(f"Gemini rate-limited ({self.gemini_model}); "
                                 f"waiting {wait:.1f}s then retrying")
                        time.sleep(wait)
                        delay = min(delay * 2, 30.0)
                        continue
                    raise

        hist = self.gemini_history.setdefault(sender, [])
        user_text = question
        if sender not in self.gemini_seeded:
            user_text = self._gemini_seed_prefix() + question
            self.gemini_seeded.add(sender)
        hist.append(types.Content(role="user",
                                  parts=[types.Part.from_text(text=user_text)]))

        # Keep history bounded, but only trim down to a clean user turn so we never
        # leave a dangling function_response without its call (Gemini rejects that).
        limit = self.cfg.history_turns * 2
        if len(hist) > limit:
            while len(hist) > limit:
                hist.pop(0)
            while hist and not (hist[0].role == "user" and hist[0].parts
                                and getattr(hist[0].parts[0], "text", None)):
                hist.pop(0)

        for _ in range(self.cfg.max_tool_iterations):
            try:
                resp = _generate(hist)
            except Exception as e:
                self.log(f"Gemini API error for {sender} ({self.gemini_model}): {e}")
                rate = _is_gemini_rate_error(e)
                # A mid-task failure poisons the tool loop (dangling calls); drop
                # the session so the next message starts clean.
                self.gemini_history.pop(sender, None)
                self.gemini_seeded.discard(sender)
                if _is_gemini_daily_error(e):
                    return self._gemini_daily_message()
                if rate:
                    return self._gemini_rate_message()
                lim = usage_limit_message(str(e), is_subscription=False)
                return lim or "אנטשולדיג, עפעס איז שיף געגאנגען מיט Gemini. פרוביר נאכאמאל."

            cand = resp.candidates[0] if getattr(resp, "candidates", None) else None
            content = cand.content if cand else None
            if content is not None:
                hist.append(content)

            fcalls = list(getattr(resp, "function_calls", None) or [])
            if fcalls:
                fr_parts, img_parts = [], []
                for fc in fcalls:
                    out = self.run_tool(fc.name, dict(fc.args or {}))
                    if isinstance(out, dict) and out.get("type") == "image":
                        fr_parts.append(types.Part.from_function_response(
                            name=fc.name,
                            response={"result": "(screenshot captured; image below)"}))
                        src = out.get("source", {})
                        try:
                            img_parts.append(types.Part.from_bytes(
                                data=base64.b64decode(src.get("data", "")),
                                mime_type=src.get("media_type", "image/png")))
                        except Exception:
                            pass
                    else:
                        fr_parts.append(types.Part.from_function_response(
                            name=fc.name, response={"result": str(out)}))
                hist.append(types.Content(role="user", parts=fr_parts))
                if img_parts:
                    hist.append(types.Content(role="user", parts=img_parts))
                continue

            text = ""
            if content is not None and content.parts:
                text = "".join(p.text for p in content.parts
                               if getattr(p, "text", None)).strip()
            reply = (text or "(קיין ענטפער)").replace("*", "").strip()
            reply = re.sub(r"\n{3,}", "\n\n", reply)
            return reply

        return "(צו פיל שריט - איך האב זיך אפגעשטעלט)"

    def list_gemini_models(self) -> str:
        """Ask the Gemini API which text-generation models this key can use."""
        try:
            client = self._gemini_client_get()
        except Exception:
            return "Gemini איז נישט גרייט - עס פעלט אן API קי (סעטאפ טאב)."
        try:
            names = []
            for m in client.models.list():
                actions = list(getattr(m, "supported_actions", None) or [])
                if actions and "generateContent" not in actions:
                    continue
                nm = (getattr(m, "name", "") or "").replace("models/", "")
                if nm.startswith("gemini") and "embedding" not in nm:
                    names.append(nm)
            names = sorted(set(names))
            if not names:
                return "קיין Gemini מאדעלן געטראפן פאר דעם API קי."
            out = f"די Gemini מאדעלן (יעצט: {self.gemini_model}):\n" + "\n".join(names)
            return out[: self.cfg.max_sms_chars]
        except Exception as e:
            self.log(f"list gemini models failed: {e}")
            return f"נישט געקענט ליסטן די Gemini מאדעלן ({type(e).__name__})."

    def current_model(self, sender: str = "") -> str:
        if self.active_provider == "gemini":
            return f"Gemini - {self.gemini_model}"
        if self._use_claude_code and sender and self.cc_mode.get(sender) == "terminal":
            model = self._terminal_model(self.cc_resume_session.get(sender, ""),
                                         self.cc_resume_cwd.get(sender) or
                                         self.cfg.claude_work_dir or "")
            return f"Claude - {model or 'CLI ברירת-המחדל'} (פון דעם טערמינאל-שמועס)"
        if self._use_claude_code:
            return f"Claude - {self.cfg.claude_code_model or 'CLI ברירת-המחדל'}"
        return f"Claude - {self.cfg.model}"

    def _forget(self, sender: str) -> None:
        self.history.pop(sender, None)
        self.cc_sessions.pop(sender, None)
        self.cc_turns.pop(sender, None)
        self.cc_mode.pop(sender, None)          # back to the casual scratch chat
        self.cc_pick.pop(sender, None)
        self.cc_pick_cwd.pop(sender, None)
        self.cc_resume_session.pop(sender, None)
        self.cc_resume_cwd.pop(sender, None)
        self.gemini_history.pop(sender, None)
        self.gemini_seeded.discard(sender)
        self.shared_history.pop(sender, None)
        self.brain_synced.pop(sender, None)

    def parse_provider_switch(self, text: str) -> Optional[tuple]:
        """If `text` is a bare provider/model switch, return (provider, model_id):
        'gemini'/'gemini 3.6'/'דשעמיני' -> ('gemini', id); 'claude'/'opus 4.8' ->
        ('claude', id). Returns None for an ordinary question."""
        low = _norm_words(text)
        if not low or len(low) > 40:
            return None
        toks = low.split()
        head = toks[0]
        rest = " ".join(toks[1:]).strip()
        if head in GEMINI_WORDS:
            return ("gemini", _resolve_gemini_model(rest))
        if head in CLAUDE_WORDS:
            return ("claude", self._resolve_model(rest.replace(" ", "")) or self.cfg.model)
        mid = self._resolve_model(low.replace(" ", ""))
        if mid:
            return ("claude", mid)
        return None

    # ================================================================== #
    #  Worker thread: local chat + optional Google Voice SMS polling      #
    # ================================================================== #
    def submit_chat(self, text: str) -> None:
        self._chat_q.put(text)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def is_running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def _run(self) -> None:
        if self.active_provider == "gemini":
            brain = f"Gemini {self.gemini_model} ({len(self.tools)} tools)"
        elif self._use_claude_code:
            brain = f"Claude Code CLI on subscription ({self._claude_code_bin()})"
        else:
            brain = f"Claude API {self.cfg.model} ({len(self.tools)} tools)"
        if (self.cfg.gemini_api_key or "").strip():
            brain += " [Gemini switch available]"
        self.log(f"Engine started. Brain: {brain}. Access: {self.cfg.access_mode}"
                 + (f" ({self.cfg.allowed_folder})" if self.cfg.access_mode == 'folder' else ""))
        if self.cfg.sms_enabled:
            self.log("Google Voice SMS bridge: ON")
        last_poll = 0.0
        allowed = {last10(n) for n in self.cfg.allowed_numbers.split(",") if last10(n)}

        while not self._stop.is_set():
            # 1) drain local chat requests
            try:
                text = self._chat_q.get_nowait()
            except queue.Empty:
                text = None
            if text is not None:
                try:
                    reply = self.handle_command("local-chat", text)
                    if reply is None:
                        reply = self.ask("local-chat", text)
                except Exception as e:
                    reply = explain_error(e)
                    self.log("Chat error: " + describe_error(e) + "\n" + traceback.format_exc())
                if self._chat_cb:
                    try:
                        self._chat_cb(reply)
                    except Exception:
                        pass

            # 2) poll Gmail for Google Voice SMS
            if self.cfg.sms_enabled and (time.time() - last_poll) >= self.cfg.poll_seconds:
                last_poll = time.time()
                try:
                    self._poll_sms(allowed)
                except Exception:
                    self.log("SMS poll error:\n" + traceback.format_exc())

            time.sleep(0.3)

        self.log("Engine stopped.")

    def _poll_sms(self, allowed: Set[str]) -> None:
        svc = self._gmail_service()
        res = svc.users().messages().list(userId="me", q=GMAIL_QUERY, maxResults=10).execute()
        messages = res.get("messages", []) or []
        new = [m for m in messages if m["id"] not in self.seen_ids]
        for m in new:
            # BUG-003: claim the id BEFORE any side-effects (tools/reply). If the
            # answer runs tools and then delivery throws, the next poll must NOT
            # re-run those tools — so we mark it seen up front, not in the except.
            self.seen_ids.add(m["id"])
            try:
                self._process_sms(svc, m["id"], allowed)
            except Exception:
                self.log(f"Failed processing {m['id']}:\n" + traceback.format_exc())

    # ---- Outbound via the Google Voice WEB UI (debug Chrome) ------------ #
    def _sender_gv_url(self, headers) -> Optional[str]:
        """Build the Google Voice conversation URL for whoever sent this SMS,
        from the phone number in the forwarding email. Returns None if we can't
        pin down exactly one number (then we play safe and use the e-mail path,
        which replies to the real Reply-To address)."""
        for hdr in ("Reply-To", "From"):
            _, addr = parseaddr(get_header(headers, hdr))
            num10 = gv_sender_number(addr)
            if num10:
                return f"https://voice.google.com/u/0/messages?itemId=t.%2B1{num10}"
        return None

    def send_via_gvoice_web(self, conv_url: str, text: str) -> bool:
        """Send `text` as an SMS by driving the Google Voice web page in the
        user's debug Chrome, reusing the engine's existing browser connection.
        This runs on the worker thread (where Playwright's sync API is safe) and
        sends the whole reply as one message — Google Voice segments it itself.
        Returns True on success; the caller falls back to e-mail on False. Never
        raises."""
        try:
            self._page_ensure()          # guarantees self._browser is connected
            browser = self._browser
            ctx = (browser.contexts[0] if browser.contexts
                   else browser.new_context())
            # Reuse an already-open Google Voice tab; close any extras so they
            # don't pile up over time. Work on the first one (or make one).
            voice = [pg for pg in ctx.pages if "voice.google.com" in pg.url]
            for extra in voice[1:]:
                try:
                    extra.close()
                except Exception:
                    pass
            page = voice[0] if voice else ctx.new_page()
            to = self.cfg.gvoice_web_timeout * 1000
            page.goto(conv_url, wait_until="domcontentloaded", timeout=to)
            box = page.locator('textarea[placeholder="Type a message"]').first
            box.wait_for(state="visible", timeout=to)
            box.click()
            box.fill(text)
            page.wait_for_timeout(500)
            send = page.locator('button[aria-label*="Send" i]').first
            if not send.is_enabled():
                page.wait_for_timeout(1000)
            send.click()
            # BUG-005: confirm the message ACTUALLY posted to the thread AND STAYED
            # there — not merely that the compose box cleared. A throttled/hidden
            # window empties the box and shows a brief OPTIMISTIC bubble that then
            # DISAPPEARS because the send never really fired. So require, in order:
            # (1) textarea empty AND a distinctive slice of our text shows up as a
            # bubble, then (2) a few seconds later that same slice is STILL there.
            # Only a real send persists. (We look only while the textarea is empty,
            # so a match comes from a sent bubble, never the compose box.)
            first_line = next((ln for ln in text.splitlines() if ln.strip()), text)
            marker = first_line.strip()[:40]
            check_js = (
                """(m) => {
                    const t = document.querySelector('textarea[placeholder="Type a message"]');
                    if (!t || t.value.trim() !== '') return false;
                    return !!(document.body && m && document.body.innerText.includes(m));
                }""")
            try:
                page.wait_for_function(check_js, arg=marker, timeout=12000)
                page.wait_for_timeout(3500)     # let a fake/optimistic bubble fall away
                sent = bool(page.evaluate(check_js, marker))
            except Exception:
                sent = False
            self.log("Sent reply via Google Voice web." if sent
                     else "gvoice_web: send did not confirm (bubble not persisted).")
            return sent
        except Exception as e:
            self.log(f"gvoice_web send failed: {e}")
            return False

    def _process_sms(self, svc, msg_id: str, allowed: Set[str]) -> None:
        original = svc.users().messages().get(userId="me", id=msg_id, format="full").execute()
        headers = original["payload"]["headers"]
        _, from_addr = parseaddr(get_header(headers, "From"))
        sender_key = from_addr.split("@")[0] if "@" in from_addr else from_addr
        raw_body, plain_raw, html_raw = get_message_text(original["payload"])

        if not sender_allowed(headers, raw_body, allowed):
            self.log(f"Ignoring SMS (not allowed): {from_addr}")
            # BUG-002: mark_read (not just in-memory seen_ids) so it leaves the
            # is:unread query — otherwise a restart re-finds and re-logs it forever.
            mark_read(svc, msg_id)
            self.seen_ids.add(msg_id)
            return

        sms_text = clean_sms_body(raw_body)
        if not sms_text:
            mark_read(svc, msg_id)
            self.seen_ids.add(msg_id)
            return
        self.log(f"SMS from {sender_key}: {sms_text[:100]}")

        reply = self.handle_command(sender_key, sms_text)
        if reply is None:
            reply = self.ask(sender_key, sms_text)

        # Final safety cap. The reply is normally split into as many numbered
        # pieces as it needs (below), so this only bites a runaway answer, and
        # then it says so explicitly instead of silently vanishing.
        if len(reply) > self.cfg.max_sms_chars:
            reply = reply[: self.cfg.max_sms_chars].rstrip() + \
                "\n...(דער ענטפער איז אפגעהאקט - פרעג 'ווייטער' פאר די רעשט)"

        # Remember the exchange for the app's "Text messages" panel.
        entry = {"time": datetime.now().isoformat(timespec="seconds"),
                 "from": gv_sender_number(from_addr) or sender_key,
                 "text": sms_text, "reply": reply,
                 "model": self.current_model(sender_key),
                 "mode": self.cc_mode.get(sender_key, "casual")}
        append_sms_log(self.data_dir, entry)
        if self._sms_cb:
            try:
                self._sms_cb(entry)
            except Exception:  # noqa: BLE001
                pass

        # Preferred path: send the whole reply through the Google Voice web page
        # in Chrome (GV segments it itself). Falls back to the e-mail path below.
        if self.cfg.reply_via == "gvoice_web":
            conv = self._sender_gv_url(headers)
            if conv and self.send_via_gvoice_web(conv, reply):
                trash_message(svc, msg_id)
                return
            self.log("gvoice_web reply unavailable; falling back to e-mail SMS.")

        chunks = split_sms(reply, self.cfg.sms_chunk_chars)
        total = len(chunks)
        self.log(f"Replying in {total} SMS piece(s) via e-mail.")
        for i, chunk in enumerate(chunks, 1):
            body = f"({i}/{total}) {chunk}" if total > 1 else chunk
            send_reply(svc, original, body)
            if i < total:
                time.sleep(self.cfg.chunk_delay_seconds)
        trash_message(svc, msg_id)


# --------------------------------------------------------------------------- #
#  Gmail / Google Voice helper functions (module level)                       #
# --------------------------------------------------------------------------- #

SMS_LOG_NAME = "sms_log.json"
SMS_LOG_KEEP = 200


def load_sms_log(data_dir) -> List[Dict[str, Any]]:
    """The recent text-message exchanges, oldest first ([] if there are none)."""
    try:
        data = json.loads((Path(data_dir) / SMS_LOG_NAME).read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def append_sms_log(data_dir, entry: Dict[str, Any]) -> None:
    """Add one exchange, keeping only the newest SMS_LOG_KEEP. Written to a temp
    file and swapped in, so a crash mid-write cannot leave half a log."""
    rows = load_sms_log(data_dir)
    rows.append(entry)
    rows = rows[-SMS_LOG_KEEP:]
    path = Path(data_dir) / SMS_LOG_NAME
    tmp = path.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass


def _decode_part(data: str) -> str:
    return base64.urlsafe_b64decode(data.encode()).decode("utf-8", errors="replace")


def extract_bodies(payload: Dict[str, Any]):
    plain, html = [], []

    def walk(p: Dict[str, Any]) -> None:
        mt = p.get("mimeType", "")
        data = p.get("body", {}).get("data")
        if data:
            if mt == "text/plain":
                plain.append(_decode_part(data))
            elif mt == "text/html":
                html.append(_decode_part(data))
        for sub in p.get("parts", []) or []:
            walk(sub)

    walk(payload)
    return "\n".join(plain), "\n".join(html)


def html_to_text(h: str) -> str:
    h = re.sub(r"(?is)<(script|style).*?</\1>", " ", h)
    h = re.sub(r"(?i)<br\s*/?>", "\n", h)
    h = re.sub(r"(?i)</(p|div|tr|li)>", "\n", h)
    h = re.sub(r"<[^>]+>", " ", h)
    h = html_lib.unescape(h)
    h = re.sub(r"[ \t]+", " ", h)
    h = re.sub(r"\n\s*\n+", "\n", h)
    return h.strip()


def get_message_text(payload: Dict[str, Any]):
    plain, html = extract_bodies(payload)
    best = plain if plain.strip() else html_to_text(html)
    return best, plain, html


FOOTER_MARKERS = ["to respond to this text message", "your account", "help center",
                  "this email was sent to you because", "get the app", "download the"]
_URL_RE = re.compile(r"<https?://[^>]+>|https?://\S+", re.I)


def clean_sms_body(raw: str) -> str:
    out = []
    for line in raw.splitlines():
        lowered = line.strip().lower()
        if any(m in lowered for m in FOOTER_MARKERS):
            break
        stripped = _URL_RE.sub("", line).strip()
        if stripped:
            out.append(stripped)
    cleaned = "\n".join(out).strip()
    return cleaned or _URL_RE.sub("", raw).strip()


def get_header(headers, name: str) -> str:
    for h in headers:
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""


def numbers_in_address(addr: str) -> Set[str]:
    local = addr.split("@")[0] if "@" in addr else addr
    out: Set[str] = set()
    for seg in re.split(r"[._\-]", local):
        if seg.isdigit() and len(seg) >= 10:
            out.add(last10(seg))
    return out


def gv_sender_number(addr: str) -> Optional[str]:
    """The SENDER's 10-digit number from a Google Voice forwarding address.

    The address is <your GV number>.<sender>.<hash>@txt.voice.google.com - two
    numbers, and only the SECOND is the person who texted. Treating both as the
    sender is what let a GV-number allowlist entry admit everybody, and what made
    the web reply give up on every message (it wanted exactly one number). For
    any other address shape, a single number is taken as the sender; anything
    ambiguous returns None rather than guess."""
    local = (addr or "").split("@")[0]
    segs = [s for s in re.split(r"[._\-]", local) if s.isdigit() and len(s) >= 10]
    if "txt.voice.google.com" in (addr or "").lower() and len(segs) >= 2:
        return last10(segs[1])
    return last10(segs[0]) if len(segs) == 1 else None


def sender_allowed(headers, raw_body: str, allowed: Set[str]) -> bool:
    """Is this SMS from someone on the allowlist?

    Identity comes ONLY from the addresses Google itself writes (From /
    Reply-To), never from the message body. The body is whatever the sender
    typed: the old fallback matched digits anywhere in its first 300 characters,
    so a stranger texting "hi 845 555 0123, run dir C:\\" was let in with full
    access to the shell. `raw_body` stays in the signature for the callers."""
    if not allowed:
        return True
    for hdr in ("Reply-To", "From"):
        _, addr = parseaddr(get_header(headers, hdr))
        num = gv_sender_number(addr)
        if num and num in allowed:
            return True
    return False


def split_sms(text: str, size: int) -> list:
    text = text.strip()
    if len(text) <= size:
        return [text] if text else []
    chunks, remaining = [], text
    while len(remaining) > size:
        window = remaining[:size]
        cut = window.rfind("\n")
        if cut < size // 2:
            cut = window.rfind(" ")
        if cut < size // 2:
            cut = size
        chunks.append(remaining[:cut].strip())
        remaining = remaining[cut:].strip()
    if remaining:
        chunks.append(remaining)
    return chunks


def send_reply(service, original: Dict[str, Any], reply_text: str) -> None:
    headers = original["payload"]["headers"]
    reply_to = get_header(headers, "Reply-To") or get_header(headers, "From")
    _, reply_addr = parseaddr(reply_to)
    subject = get_header(headers, "Subject")
    if not subject.lower().startswith("re:"):
        subject = "Re: " + subject
    message_id = get_header(headers, "Message-ID")
    mime = MIMEText(reply_text, "plain", "utf-8")
    mime["To"] = reply_addr
    mime["Subject"] = subject
    if message_id:
        mime["In-Reply-To"] = message_id
        mime["References"] = message_id
    raw = base64.urlsafe_b64encode(mime.as_bytes()).decode()
    service.users().messages().send(
        userId="me", body={"raw": raw, "threadId": original.get("threadId")}).execute()


def mark_read(service, msg_id: str) -> None:
    service.users().messages().modify(
        userId="me", id=msg_id, body={"removeLabelIds": ["UNREAD"]}).execute()


def trash_message(service, msg_id: str) -> None:
    try:
        service.users().messages().trash(userId="me", id=msg_id).execute()
    except Exception:
        mark_read(service, msg_id)
