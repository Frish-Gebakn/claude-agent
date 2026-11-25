"""
MyDeskAI  --  desktop app around agent_core.AgentEngine.

A single-window program that lets anyone run a Claude agent with access to their
computer, their Chrome browser, and optional Gmail / Google Drive / GitHub /
custom connectors. The user supplies an Anthropic API key (or uses their Claude
subscription through the Claude Code CLI) and, for the Google features, a Gmail
OAuth credentials.json.

The window is a web page (ui/index.html) shown by Microsoft Edge WebView2 through
pywebview - real CSS, so shadows, motion and right-to-left Yiddish work, which
Tkinter could not do. The page calls the Api class below for everything and polls
Api.poll() for what happened since (a chat reply, a step the agent took, an SMS).

Settings persist to config.json next to the executable.
"""

import dataclasses
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import platform

import agent_core
import logsend
from agent_core import AgentConfig, AgentEngine

APP_TITLE = "MyDeskAI"
APP_VERSION = "1.15.0"
# A --windowed exe has no console; spawning a console child (cmd/powershell) pops
# a black window and can steal focus. CREATE_NO_WINDOW suppresses it (Windows only).
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
CREDIT = "קרעדיט@פריש געבאקן אייוועלט"
# Official update source — baked in so auto-update works out of the box.
DEFAULT_UPDATE_REPO = "Frish-Gebakn/claude-agent"
# How long a restricted command waits for Approve/Deny before it counts as Deny.
APPROVAL_TIMEOUT = 180
# The model dropdown, newest first: (id, name, one line about it). When an API key
# is set, Api.models() asks Anthropic for the live list and this only supplies the
# descriptions; without a key (subscription mode) it IS the list.
MODEL_CHOICES = [
    ("claude-fable-5-1", "Fable 5.1", "The most capable"),
    ("claude-opus-5-5", "Opus 5.5", "Very capable, a good everyday choice"),
    ("claude-sonnet-5-5", "Sonnet 5.5", "Fast and capable"),
    ("claude-opus-5", "Opus 5", "The previous Opus"),
    ("claude-fable-5", "Fable 5", "The previous Fable"),
    ("claude-sonnet-5", "Sonnet 5", "The previous Sonnet"),
    ("claude-opus-4-8", "Opus 4.8", "Older"),
    ("claude-haiku-4-5", "Haiku 4.5", "The fastest and cheapest"),
]
MODELS = [m[0] for m in MODEL_CHOICES]
# What a new install runs on. Deliberately not the top of the list: the most
# capable model is also the most expensive per token.
DEFAULT_MODEL = "claude-opus-5"
WEBVIEW2_URL = "https://go.microsoft.com/fwlink/p/?LinkId=2124703"

GOOGLE_HELP = """HOW TO CREATE THE GOOGLE "credentials.json" (for Gmail / Drive / SMS)
======================================================================

You only need this if you want the Gmail, Google Drive, or Google Voice SMS
features. It is a free, one-time setup in the Google Cloud Console.

STEP 1 — Create (or pick) a Google Cloud project
  1. Go to  https://console.cloud.google.com/
  2. Sign in with the SAME Google account whose Gmail/Drive you want the AI
     to use.
  3. Top bar → project dropdown → "New Project" → give it any name
     (e.g. "Claude Agent") → Create. Wait a few seconds, then select it.

STEP 2 — Enable the APIs you need
  1. Left menu → "APIs & Services" → "Library".
  2. Search "Gmail API"  → click it → "Enable".
  3. Search "Google Drive API" → click it → "Enable".
     (Enable Drive only if you want the Drive connector. Gmail is enough for
     email and for the Google Voice SMS bridge.)

STEP 3 — Configure the OAuth consent screen
  1. Left menu → "APIs & Services" → "OAuth consent screen".
  2. User type: choose "External" → Create.
  3. Fill App name (e.g. "Claude Agent"), your email for support + developer
     contact. Save and continue.
  4. Scopes page: you can just Save and continue (no need to add scopes here).
  5. "Test users" page → "Add users" → add YOUR Google email address.
     (While the app is in "Testing", only listed test users can log in — that
     is fine for personal use.) Save and continue.

STEP 4 — Create the OAuth client (this makes the JSON file)
  1. Left menu → "APIs & Services" → "Credentials".
  2. "Create Credentials" → "OAuth client ID".
  3. Application type: "Desktop app". Name it anything → Create.
  4. A popup shows the client. Click "Download JSON".
  5. That downloaded file IS your credentials.json.

STEP 5 — Load it into this app
  1. Setup tab → "Browse…" → pick the JSON you just downloaded.
     (The app copies it next to the program as credentials.json.)
  2. Click "Connect Google Account". A browser opens.
  3. Pick your Google account. You may see "Google hasn't verified this app" —
     click "Advanced" → "Go to Claude Agent (unsafe)" → Continue. This warning
     is normal for your own personal OAuth app.
  4. Approve the Gmail (and Drive) permissions. Done — a token.json is saved
     and the status shows "Connected as your@email".

FOR THE GOOGLE VOICE SMS BRIDGE (optional)
  - Use a Google Voice number (voice.google.com) on the same Google account.
  - In Google Voice → Settings → Messages → turn ON "Forward messages to
    email". Now texts arrive in that Gmail, and the app can read & reply so
    the AI answers by SMS.

TROUBLESHOOTING
  - "access_denied" at login → add your email under Test users (Step 3.5).
  - "Gmail API has not been used / disabled" → finish Step 2 (Enable APIs).
  - Wrong account → delete token.json next to the program and reconnect.
======================================================================"""


def base_dir() -> Path:
    """Folder next to the .exe (frozen) or next to this script (dev). Used for
    WRITABLE files that live beside the program (config.json, token.json, …)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def resource_dir() -> Path:
    """Folder holding bundled READ-ONLY assets (nav icons, app icon). In a frozen
    onefile build these are extracted to sys._MEIPASS, not the exe's folder."""
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
    return Path(__file__).resolve().parent


CONFIG_PATH = base_dir() / "config.json"
DATA_DIR = base_dir()
CHROME_PROFILE = base_dir() / "ChromeProfile"


def find_chrome() -> str:
    for p in (r"C:\Program Files\Google\Chrome\Application\chrome.exe",
              r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
              os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe")):
        if os.path.exists(p):
            return p
    return ""


# --------------------------------------------------------------------------- #
#  Self-updating (no external tools needed — everything is inside the .exe)    #
# --------------------------------------------------------------------------- #
STARTUP_LNK = "Claude Agent Update Check.lnk"


def _ver_tuple(v):
    nums = re.findall(r"\d+", v or "")
    return tuple(int(n) for n in nums) if nums else (0,)


def check_latest(repo):
    """Query the repo's latest GitHub release. Returns {tag,url,notes} if it is
    newer than APP_VERSION, else None. Raises on network error."""
    import json as _json
    import urllib.request
    url = f"https://api.github.com/repos/{repo}/releases/latest"
    req = urllib.request.Request(url, headers={"User-Agent": APP_TITLE})
    with urllib.request.urlopen(req, timeout=20) as r:
        data = _json.loads(r.read().decode())
    tag = data.get("tag_name", "")
    if _ver_tuple(tag) > _ver_tuple(APP_VERSION):
        exes = [a for a in data.get("assets", [])
                if a.get("name", "").lower().endswith(".exe")]
        # Prefer the windowed build if a console build ships alongside it.
        exes.sort(key=lambda a: "console" in a.get("name", "").lower())
        a = exes[0] if exes else {}
        return {"tag": tag, "url": a.get("browser_download_url", ""),
                "size": int(a.get("size") or 0),
                "notes": (data.get("body", "") or "")[:500]}
    return None


def download_and_swap(url, expected_size=0):
    """Download the new .exe, PROVE it is one, then hand off to a .bat that waits
    for us to exit, swaps it in with a backup, and relaunches.

    The old version wrote whatever came back with HTTP 200 straight over the exe.
    This house's content filter answers blocked downloads with 200 and an HTML
    page - which would have replaced the program with a web page and left no way
    back. Now: the size must match the release asset, the file must start with
    the Windows executable signature, the old exe is kept as .bak, and a move
    that fails (the onefile bootloader can still hold the file) is retried and,
    if it never succeeds, the backup is put back."""
    import urllib.request
    cur = Path(sys.executable)
    new = cur.with_name(cur.stem + " (update).exe")
    bak = cur.with_name(cur.stem + ".bak")
    req = urllib.request.Request(url, headers={"User-Agent": APP_TITLE})
    with urllib.request.urlopen(req, timeout=300) as r:
        blob = r.read()
    if expected_size and len(blob) != expected_size:
        raise RuntimeError(f"download is {len(blob)} bytes, the release says "
                           f"{expected_size} - not installing it")
    if blob[:2] != b"MZ":
        raise RuntimeError("the download is not a Windows program (blocked or "
                           "replaced on the way?) - not installing it")
    new.write_bytes(blob)
    bat = cur.with_name("_update.bat")
    bat.write_text(
        "@echo off\r\n"
        f'copy /Y "{cur.name}" "{bak.name}" >nul\r\n'
        "set n=0\r\n"
        ":retry\r\n"
        "ping -n 3 127.0.0.1 >nul\r\n"
        f'move /Y "{new.name}" "{cur.name}" >nul 2>&1\r\n'
        f'if exist "{new.name}" (\r\n'
        "  set /a n+=1\r\n"
        "  if %n% LSS 15 goto retry\r\n"
        f'  copy /Y "{bak.name}" "{cur.name}" >nul\r\n'
        f'  del "{new.name}" >nul 2>&1\r\n'
        ")\r\n"
        f'start "" "{cur.name}"\r\n'
        'del "%~f0"\r\n',
        encoding="ascii")
    subprocess.Popen(["cmd", "/c", str(bat)], cwd=str(cur.parent),
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, creationflags=_NO_WINDOW)


def register_update_task():
    """Install a per-user Startup shortcut that runs a silent update check at each
    logon. The Startup folder is writable without admin — no Scheduled Task (which
    needs elevation here) and no extra program. Idempotent."""
    if not getattr(sys, "frozen", False):
        return
    startup_dir = os.path.expandvars(
        r"%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup")
    lnk = os.path.join(startup_dir, STARTUP_LNK)
    if os.path.exists(lnk):
        return  # already installed
    exe = sys.executable.replace("'", "''")
    workdir = str(base_dir()).replace("'", "''")
    lnk_ps = lnk.replace("'", "''")
    ps = f'''
$W = New-Object -ComObject WScript.Shell
$s = $W.CreateShortcut('{lnk_ps}')
$s.TargetPath = '{exe}'
$s.Arguments = '--update-notify'
$s.WorkingDirectory = '{workdir}'
$s.IconLocation = '{exe}'
$s.Save()
'''
    try:
        subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                       capture_output=True, timeout=30, stdin=subprocess.DEVNULL,
                       creationflags=_NO_WINDOW)
    except Exception:
        pass


def update_notify():
    """Silent background check (run by the scheduled task). Shows a small dialog
    ONLY if an update exists; otherwise exits with no window."""
    try:
        d = json.loads((base_dir() / "config.json").read_text(encoding="utf-8-sig"))
    except Exception:
        return
    if not d.get("auto_check_updates", True):
        return
    repo = (d.get("update_repo") or "").strip()
    if not repo:
        return
    try:
        info = check_latest(repo)
    except Exception:
        return
    if not info or not info.get("url"):
        return
    import tkinter as tk
    from tkinter import messagebox
    root = tk.Tk()
    root.withdraw()
    if messagebox.askyesno(
            APP_TITLE,
            f"A new version ({info['tag']}) of {APP_TITLE} is available "
            f"(you have v{APP_VERSION}).\n\n{info.get('notes','')}\n\n"
            "Download and install it now?"):
        try:
            download_and_swap(info["url"], info.get("size", 0))
        except Exception as e:
            messagebox.showerror(APP_TITLE, f"Update failed: {e}")
    root.destroy()


# --------------------------------------------------------------------------- #
#  Settings <-> the page                                                       #
# --------------------------------------------------------------------------- #
# Every setting the page shows, with its default. The names match AgentConfig
# fields except the two friendly booleans reply_via_web / browser_in_subscription.
UI_DEFAULTS = {
    "anthropic_api_key": "", "gemini_api_key": "", "model": DEFAULT_MODEL,
    "ai_backend": "api", "claude_code_model": "", "claude_work_dir": "", "effort": "",
    "autonomous": False, "browser_in_subscription": False, "max_tool_iterations": "50",
    "credentials_file": "", "access_mode": "full", "allowed_folder": "",
    "gmail_enabled": False, "gdrive_enabled": False, "sms_enabled": False,
    "allowed_numbers": "", "reply_via_web": True, "github_enabled": False,
    "github_token": "", "custom_enabled": False, "custom_name": "custom_api",
    "custom_base_url": "", "custom_auth_header": "", "custom_description": "",
    "update_repo": DEFAULT_UPDATE_REPO, "auto_check_updates": True,
    "restricted_commands": "", "system_instructions": "",
}
_BOOL_KEYS = {k for k, v in UI_DEFAULTS.items() if isinstance(v, bool)}


def values_from_config(cfg: AgentConfig) -> dict:
    vals = dict(UI_DEFAULTS)
    for key in vals:
        if hasattr(cfg, key):
            vals[key] = getattr(cfg, key)
    vals["max_tool_iterations"] = str(cfg.max_tool_iterations)
    # friendly booleans over cfg.reply_via ("gvoice_web"/"gmail") and the MCP path
    vals["reply_via_web"] = cfg.reply_via != "gmail"
    vals["browser_in_subscription"] = bool((cfg.claude_code_mcp_config or "").strip())
    return vals


def clean_values(values: dict) -> dict:
    """What came back from the page, typed like UI_DEFAULTS (unknown keys dropped)."""
    out = dict(UI_DEFAULTS)
    for k, v in (values or {}).items():
        if k not in out:
            continue
        out[k] = bool(v) if k in _BOOL_KEYS else ("" if v is None else str(v))
    return out


def find_playwright_mcp() -> str:
    """The `playwright-mcp` launcher (installed via `npm i -g @playwright/mcp`):
    an absolute path if we can find it, else the bare name (relies on PATH)."""
    cand = os.path.expandvars(r"%APPDATA%\npm\playwright-mcp.cmd")
    if os.path.exists(cand):
        return cand
    return shutil.which("playwright-mcp") or shutil.which("playwright-mcp.cmd") \
        or "playwright-mcp.cmd"


def ensure_mcp_config() -> str:
    """Write an MCP-servers file next to the app that runs the Playwright MCP
    against the debug Chrome (port 9222), and return its path. This is what gives
    the Claude Code CLI (subscription mode) control of the user's Chrome."""
    path = base_dir() / "mcp_playwright.json"
    data = {"mcpServers": {"playwright": {"command": "cmd", "args": [
        "/c", find_playwright_mcp(), "--cdp-endpoint", "http://127.0.0.1:9222"]}}}
    try:
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception:
        pass
    return str(path)


def build_config(values: dict, raw: dict) -> AgentConfig:
    """The page's values -> an AgentConfig, keeping everything the page does not
    show exactly as config.json has it."""
    v = clean_values(values)
    try:
        iters = int(v["max_tool_iterations"])
    except ValueError:
        iters = 50
    raw = raw or {}
    # Browser control in subscription mode = generate the Playwright MCP config
    # and point the CLI at it; off = no MCP (Claude Code's own tools only).
    cc_mcp = (ensure_mcp_config()
              if v["ai_backend"] == "claude_code" and v["browser_in_subscription"] else "")
    kw = dict(
        anthropic_api_key=v["anthropic_api_key"].strip(),
        gemini_api_key=v["gemini_api_key"].strip(),
        model=v["model"].strip() or DEFAULT_MODEL,
        ai_backend=v["ai_backend"].strip() or "api",
        claude_code_model=v["claude_code_model"].strip(),
        claude_work_dir=v["claude_work_dir"].strip(),
        effort=(v["effort"].strip().lower()
                if v["effort"].strip().lower() in agent_core.EFFORT_LEVELS else ""),
        autonomous=v["autonomous"],
        claude_code_mcp_config=cc_mcp,
        max_tool_iterations=iters,
        system_instructions=v["system_instructions"].strip(),
        access_mode=v["access_mode"],
        allowed_folder=v["allowed_folder"].strip(),
        restricted_commands=v["restricted_commands"].strip(),
        credentials_file=v["credentials_file"].strip(),
        gmail_enabled=v["gmail_enabled"],
        gdrive_enabled=v["gdrive_enabled"],
        sms_enabled=v["sms_enabled"],
        allowed_numbers=v["allowed_numbers"].strip(),
        reply_via=("gvoice_web" if v["reply_via_web"] else "gmail"),
        github_enabled=v["github_enabled"],
        github_token=v["github_token"].strip(),
        custom_enabled=v["custom_enabled"],
        custom_name=v["custom_name"].strip() or "custom_api",
        custom_base_url=v["custom_base_url"].strip(),
        custom_auth_header=v["custom_auth_header"].strip(),
        custom_description=v["custom_description"].strip(),
        update_repo=v["update_repo"].strip() or DEFAULT_UPDATE_REPO,
        auto_check_updates=v["auto_check_updates"],
    )
    cfg = AgentConfig(**kw)
    # Every AgentConfig field the page does NOT set keeps its value from
    # config.json. Listing them by hand had missed eleven (tool_timeout,
    # chrome_debug_port, poll_seconds, gemini_model, max_tokens, ...), so each
    # Save quietly put them back to defaults - and Start saves automatically.
    if raw:
        base = AgentConfig.from_dict(raw)
        for f in dataclasses.fields(AgentConfig):
            if f.name not in kw:
                setattr(cfg, f.name, getattr(base, f.name))
        # a custom MCP config path the user wrote by hand also survives
        if not cc_mcp and raw.get("claude_code_mcp_config") and \
                v["ai_backend"] == "claude_code" and v["browser_in_subscription"]:
            cfg.claude_code_mcp_config = str(raw["claude_code_mcp_config"])
    return cfg


def pretty_model(model_id: str) -> str:
    """claude-opus-5-5 -> Opus 5.5, claude-haiku-4-5 -> Haiku 4.5."""
    m = (model_id or "").strip().replace("claude-", "")
    m = re.sub(r"(\d)-(\d)", r"\1.\2", m)
    return m.replace("-", " ").title()


# What the agent is doing, from its log lines: (prefix, icon, what the user sees).
STEP_RULES = [
    ("run_cmd:", "terminal", "Ran a command"),
    ("run_powershell:", "terminal", "Ran PowerShell"),
    ("read_file:", "file", "Read a file"),
    ("write_file:", "file", "Wrote a file"),
    ("edit_file:", "file", "Edited a file"),
    ("list_directory:", "folder", "Looked in a folder"),
    ("browser_navigate:", "globe", "Opened a page in Chrome"),
    ("browser_click:", "globe", "Clicked on a page"),
    ("browser_fill:", "globe", "Filled in a form"),
    ("browser_js:", "globe", "Ran a script on a page"),
    ("gmail_search:", "mail", "Searched Gmail"),
    ("gmail_read:", "mail", "Read an e-mail"),
    ("gmail_send:", "mail", "Sent an e-mail"),
    ("drive_", "cloud", "Used Google Drive"),
    ("github_", "code", "Used GitHub"),
    ("custom_api:", "plug", "Called your web API"),
    ("SMS from", "phone", "Got a text message"),
    ("claude -p", "spark", "Working in Claude Code"),
]


def step_from_log(line: str):
    """A log line -> {icon, title, detail} when it is a step worth showing."""
    for prefix, icon, title in STEP_RULES:
        if line.startswith(prefix):
            detail = line[len(prefix):].strip().lstrip(":").strip()
            return {"icon": icon, "title": title, "detail": detail[:90]}
    return None


# --------------------------------------------------------------------------- #
#  The bridge the page talks to                                                #
# --------------------------------------------------------------------------- #
class Api:
    """Everything the page can ask for. pywebview runs these on its own threads;
    the page polls poll() a few times a second for what happened in between.
    (Names starting with "_" are not visible to the page.)"""

    def __init__(self, config_path: Path = None, data_dir: Path = None,
                 engine_cls=None):
        self._config_path = Path(config_path or CONFIG_PATH)
        self._data_dir = Path(data_dir or DATA_DIR)
        self._engine_cls = engine_cls or AgentEngine
        self._window = None
        self._engine = None
        self._engine_on = False
        self._events: "queue.Queue[dict]" = queue.Queue()
        self._approvals = {}                 # id -> (threading.Event, holder)
        self._raw_cfg: dict = {}
        self._values = self._load_values()

    # ---- plumbing ------------------------------------------------------- #
    def _emit(self, kind: str, **data) -> None:
        data["kind"] = kind
        self._events.put(data)

    def _load_values(self) -> dict:
        if not self._config_path.exists():
            vals = dict(UI_DEFAULTS)
            cand = self._config_path.with_name("credentials.json")
            if cand.exists():
                vals["credentials_file"] = str(cand)
            return vals
        try:
            # utf-8-sig tolerates a BOM (config.json saved by PowerShell/Notepad)
            d = json.loads(self._config_path.read_text(encoding="utf-8-sig"))
        except Exception as e:  # noqa: BLE001
            bad = self._config_path.with_suffix(".broken.json")
            try:
                shutil.copy2(self._config_path, bad)
            except OSError:
                pass
            self._emit("info", title="Settings could not be read",
                       text=f"config.json could not be read ({e}). A copy was kept "
                            f"as {bad.name}, so nothing is lost. Fix it, or set "
                            "things up again and press Save.")
            return dict(UI_DEFAULTS)
        self._raw_cfg = d          # keep raw so Save preserves what the page lacks
        return values_from_config(AgentConfig.from_dict(d))

    def _running(self) -> bool:
        return bool(self._engine_on and self._engine and self._engine.is_running())

    def _model_label(self, values=None) -> str:
        v = values or self._values
        if self._running():
            try:
                lab = self._engine.current_model()          # "Claude - claude-opus-5"
                brain, _, mid = lab.partition(" - ")
                return f"{brain} {pretty_model(mid)}" if brain == "Claude" else lab
            except Exception:  # noqa: BLE001
                pass
        if v.get("ai_backend") == "claude_code":
            m = pretty_model(v.get("claude_code_model", ""))
            return "Claude subscription" + (f", {m}" if m else "")
        return "Claude " + (pretty_model(v.get("model", "")) or "API")

    def _state(self) -> dict:
        return {"running": self._running(), "model": self._model_label()}

    def _write_config(self, cfg: AgentConfig) -> None:
        self._config_path.write_text(json.dumps(cfg.to_dict(), indent=2), encoding="utf-8")
        self._raw_cfg = cfg.to_dict()

    # ---- what the page calls --------------------------------------------- #
    def init(self) -> dict:
        return {"title": APP_TITLE, "version": APP_VERSION, "credit": CREDIT,
                "models": [{"id": i, "label": n, "note": t} for i, n, t in MODEL_CHOICES],
                "values": self._values,
                "sms": agent_core.load_sms_log(self._data_dir)[-80:],
                "frozen": bool(getattr(sys, "frozen", False)),
                "approvalTimeout": APPROVAL_TIMEOUT, "googleHelp": GOOGLE_HELP,
                **self._state()}

    def poll(self) -> list:
        """Everything that happened since the last call (oldest first)."""
        if self._engine_on and not (self._engine and self._engine.is_running()):
            # the worker died on its own (a crash, a fatal API error)
            self._engine_on = False
            self._emit("status", note="The agent stopped - see the activity log.",
                       **self._state())
        out = []
        try:
            while len(out) < 200:
                out.append(self._events.get_nowait())
        except queue.Empty:
            pass
        return out

    def save_settings(self, values: dict) -> dict:
        self._values = clean_values(values)
        try:
            self._write_config(build_config(self._values, self._raw_cfg))
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "message": f"Could not save settings: {e}"}
        return {"ok": True, "message": "Settings saved.", **self._state()}

    def start(self, values: dict = None) -> dict:
        if values is not None:
            self._values = clean_values(values)
        if self._running():
            return {"ok": True, **self._state()}
        cfg = build_config(self._values, self._raw_cfg)
        if cfg.ai_backend == "api" and not cfg.anthropic_api_key.strip():
            return {"ok": False, "reason": "no_key",
                    "message": "There is no Anthropic API key yet. Add one in Settings, "
                               "or pick Claude subscription there to run without one."}
        try:
            self._write_config(cfg)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "reason": "error", "message": f"Could not save settings: {e}"}
        try:
            self._engine = self._engine_cls(
                cfg, self._data_dir,
                log_cb=self._on_log,
                chat_cb=lambda text: self._emit("chat", text=text),
                approval_cb=self._ask_approval,
                sms_cb=lambda entry: self._emit("sms", entry=entry))
            self._engine.log(f"{APP_TITLE} v{APP_VERSION} on {platform.platform()}")
            self._engine.start()
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "reason": "error", "message": f"Could not start: {e}"}
        self._engine_on = True
        return {"ok": True, **self._state()}

    def stop(self) -> dict:
        if self._engine:
            self._engine.stop()
        self._engine_on = False
        return {"ok": True, **self._state()}

    def send(self, text: str, values: dict = None) -> dict:
        """Send a chat message, starting the agent first if it is not running."""
        text = (text or "").strip()
        if not text:
            return {"ok": False, "reason": "empty"}
        if not self._running():
            r = self.start(values)
            if not r.get("ok"):
                return r
        self._engine.submit_chat(text)
        return {"ok": True, **self._state()}

    def clear_chat(self) -> dict:
        if self._engine:
            self._engine.history.pop("local-chat", None)
        return {"ok": True}

    def sms_log(self) -> list:
        return agent_core.load_sms_log(self._data_dir)[-80:]

    def models(self, values: dict = None) -> dict:
        """The choices for the model dropdowns: [{id, label, note}], newest first.
        With an API key, Anthropic's own list (so a model released next month
        shows up without an update); otherwise, or if that call fails, the
        built-in MODEL_CHOICES."""
        notes = {mid: (name, note) for mid, name, note in MODEL_CHOICES}
        static = [{"id": mid, "label": name, "note": note} for mid, name, note in MODEL_CHOICES]
        key = (clean_values(values or self._values)["anthropic_api_key"] or "").strip()
        if not key:
            return {"choices": static, "live": False}
        try:
            from anthropic import Anthropic
            ids = [m.id for m in Anthropic(api_key=key, timeout=8).models.list(limit=100)
                   if str(m.id).startswith("claude-")]
        except Exception:  # noqa: BLE001 - offline, filtered, bad key: use the built-in list
            return {"choices": static, "live": False}
        if not ids:
            return {"choices": static, "live": False}
        out = []
        for mid in ids:                      # Anthropic lists newest first
            name, note = notes.get(mid, (pretty_model(re.sub(r"-\d{8}$", "", mid)), ""))
            out.append({"id": mid, "label": name, "note": note})
        return {"choices": out, "live": True}

    def set_model(self, model_id: str, values: dict = None) -> dict:
        """The quick switch under the chat box: save the choice and, if the agent
        is running, use it from the next message on."""
        v = clean_values(values or self._values)
        key = "claude_code_model" if v["ai_backend"] == "claude_code" else "model"
        v[key] = model_id or ""
        r = self.save_settings(v)
        if r.get("ok") and self._running():
            try:
                setattr(self._engine.cfg, key, model_id or "")
                self._engine.active_provider = "claude"
            except Exception:  # noqa: BLE001
                pass
        return {**r, "key": key, "values": self._values, **self._state()}

    def set_effort(self, level: str, values: dict = None) -> dict:
        """The effort chip under the chat box: save it and, if the agent is
        running, use it from the next message on."""
        level = (level or "").strip().lower()
        if level not in agent_core.EFFORT_LEVELS:
            level = ""
        v = clean_values(values or self._values)
        v["effort"] = level
        r = self.save_settings(v)
        if r.get("ok") and self._running():
            try:
                self._engine.cfg.effort = level
            except Exception:  # noqa: BLE001
                pass
        return {**r, "values": self._values, **self._state()}

    def answer_approval(self, aid: str, ok: bool) -> dict:
        item = self._approvals.pop(aid, None)
        if item:
            ev, holder = item
            holder["ok"] = bool(ok)
            ev.set()
            self._emit("log", line=("APPROVED: " if ok else "DENIED: ") + holder["cmd"][:80])
        return {"ok": bool(item)}

    def pick(self, kind: str) -> str:
        """A file or folder chooser. kind: credentials | folder | workdir."""
        if not self._window:
            return ""
        import webview
        if kind == "credentials":
            res = self._window.create_file_dialog(
                webview.OPEN_DIALOG, file_types=("JSON files (*.json)", "All files (*.*)"))
            if not res:
                return ""
            src = res[0] if isinstance(res, (list, tuple)) else res
            dest = base_dir() / "credentials.json"   # travels with the config
            try:
                if os.path.abspath(src) != os.path.abspath(dest):
                    shutil.copyfile(src, dest)
                return str(dest)
            except Exception as e:  # noqa: BLE001
                self._emit("info", title="Could not copy the file", text=str(e))
                return ""
        res = self._window.create_file_dialog(webview.FOLDER_DIALOG)
        if not res:
            return ""
        return res[0] if isinstance(res, (list, tuple)) else res

    def connect_google(self, values: dict) -> dict:
        saved = self.save_settings(values)
        if not saved.get("ok"):
            return saved
        cfg = build_config(self._values, self._raw_cfg)
        if not cfg.credentials_file:
            return {"ok": False, "message": "Pick your Google credentials.json first."}

        def work():
            try:
                msg = AgentEngine(cfg, self._data_dir).connect_google_interactive()
            except Exception as e:  # noqa: BLE001
                msg = f"Failed: {e}"
            self._emit("google", text=msg)
        threading.Thread(target=work, daemon=True).start()
        return {"ok": True, "message": "A browser is opening - finish the sign-in there."}

    def open_chrome(self) -> dict:
        chrome = find_chrome()
        if not chrome:
            return {"ok": False, "message": "Chrome not found. Install Google Chrome."}
        try:
            CHROME_PROFILE.mkdir(parents=True, exist_ok=True)
            subprocess.Popen([chrome, "--remote-debugging-port=9222",
                              f"--user-data-dir={CHROME_PROFILE}",
                              "--profile-directory=Default",
                              "--no-first-run", "--no-default-browser-check"],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "message": f"Could not launch Chrome: {e}"}
        return {"ok": True, "message": "Chrome is open for the AI. Sign in to the sites "
                                       "you want it to use."}

    def make_shortcuts(self) -> dict:
        threading.Thread(target=self._shortcuts_work, daemon=True).start()
        return {"ok": True, "message": "Adding shortcuts…"}

    def check_updates(self, manual: bool = False) -> dict:
        repo = (self._values.get("update_repo") or "").strip()
        if not repo:
            return {"ok": False, "message": "Set a GitHub owner/repo first."}

        def work():
            try:
                info = check_latest(repo)
                if info:
                    self._emit("update", info=info)
                elif manual:
                    self._emit("toast", text=f"You're up to date (v{APP_VERSION}).")
            except Exception as e:  # noqa: BLE001
                if manual:
                    self._emit("toast", text=f"Update check failed: {e}")
        threading.Thread(target=work, daemon=True).start()
        return {"ok": True}

    def do_update(self, info: dict) -> dict:
        if not getattr(sys, "frozen", False):
            return {"ok": False, "message": "Auto-install works only in the built .exe. "
                                            f"Version {info.get('tag')} is at {info.get('url')}"}
        if not info.get("url"):
            return {"ok": False, "message": f"Version {info.get('tag')} has no .exe to install."}

        def work():
            try:
                download_and_swap(info["url"], info.get("size", 0))
                self._emit("quit")          # the page asks us to close
            except Exception as e:  # noqa: BLE001
                self._emit("info", title="Update failed", text=str(e))
        threading.Thread(target=work, daemon=True).start()
        return {"ok": True, "message": f"Downloading {info.get('tag')}…"}

    # ---- Send logs ------------------------------------------------------- #
    def _log_report(self, note: str = "") -> str:
        return logsend.build_report(self._data_dir, self._values, APP_TITLE, APP_VERSION,
                                    bool(getattr(sys, "frozen", False)), note)

    def logs_preview(self) -> dict:
        """Exactly what Send would send (secrets already removed), so the user
        can read it first."""
        try:
            return {"ok": True, "text": self._log_report("")}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "message": f"Could not read the log: {e}"}

    def send_logs(self, note: str = "") -> dict:
        try:
            report = self._log_report(note)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "message": f"Could not read the log: {e}"}
        try:
            rid = logsend.upload(report, APP_TITLE, APP_VERSION)
        except logsend.UploadFailed as e:
            # Never lose it: the same report goes on the Desktop to send by hand.
            try:
                path = logsend.save_copy(report, APP_TITLE)
            except Exception as e2:  # noqa: BLE001
                return {"ok": False, "message": f"Sending failed ({e}), and saving a copy "
                                                f"failed too ({e2})."}
            return {"ok": False, "saved": str(path),
                    "message": f"Sending failed: {e}.\n\nThe same log was saved on your "
                               f"Desktop as:\n{path.name}\n\nYou can send that file to the "
                               "developer yourself."}
        return {"ok": True, "id": rid}

    def save_logs(self, note: str = "") -> dict:
        try:
            path = logsend.save_copy(self._log_report(note), APP_TITLE)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "message": f"Could not save: {e}"}
        return {"ok": True, "path": str(path), "message": f"Saved on your Desktop: {path.name}"}

    def quit(self) -> None:
        if self._window:
            self._window.destroy()

    # ---- engine callbacks (on the engine's worker thread) ------------------ #
    def _on_log(self, line: str) -> None:
        self._emit("log", line=line)
        step = step_from_log(line)
        if step:
            self._emit("step", **step)

    def _ask_approval(self, command: str) -> bool:
        """Blocks the worker until the page answers Allow/Deny."""
        aid = uuid.uuid4().hex[:12]
        ev = threading.Event()
        holder = {"ok": False, "cmd": command}
        self._approvals[aid] = (ev, holder)
        self._emit("approve", id=aid, command=command, timeout=APPROVAL_TIMEOUT)
        # An SMS can ask for a restricted command while nobody is at the PC. With
        # no limit the whole engine froze: no polling, no chat, no reply to the
        # texter, and Stop could not help. Unanswered means NO.
        if not ev.wait(timeout=APPROVAL_TIMEOUT):
            self._approvals.pop(aid, None)
            self._emit("approve_gone", id=aid)
            self._emit("log", line=f"DENIED (no answer in {APPROVAL_TIMEOUT}s): {command[:80]}")
            return False
        return holder["ok"]

    def _on_closing(self) -> None:
        if self._engine:
            self._engine.stop()

    def _shortcuts_work(self) -> None:
        if getattr(sys, "frozen", False):
            exe, args, workdir = sys.executable, "", str(base_dir())
        else:
            pyw = str(Path(sys.executable).with_name("pythonw.exe"))
            exe = pyw if os.path.exists(pyw) else sys.executable
            args, workdir = f'"{base_dir() / "app.py"}"', str(base_dir())
        icon = str(base_dir() / "app_icon.ico")
        if not os.path.exists(icon):
            icon = exe
        start_menu = Path(os.path.expandvars(
            r"%APPDATA%\Microsoft\Windows\Start Menu\Programs")) / f"{APP_TITLE}.lnk"
        desktop = Path(os.path.expandvars(r"%USERPROFILE%\Desktop")) / f"{APP_TITLE}.lnk"

        def q(s):                       # a PowerShell single-quoted string
            return "'" + str(s).replace("'", "''") + "'"
        ps = f'''
$paths = @({q(start_menu)}, {q(desktop)})
$W = New-Object -ComObject WScript.Shell
foreach ($p in $paths) {{
  $s = $W.CreateShortcut($p)
  $s.TargetPath = {q(exe)}
  $s.Arguments = {q(args)}
  $s.WorkingDirectory = {q(workdir)}
  $s.IconLocation = {q(icon)}
  $s.Save()
}}
try {{
  $sh = New-Object -ComObject Shell.Application
  $f = $sh.Namespace((Split-Path {q(start_menu)}))
  $i = $f.ParseName((Split-Path {q(start_menu)} -Leaf))
  $i.InvokeVerb('taskbarpin')
}} catch {{}}
'''
        try:
            subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                           capture_output=True, timeout=30, stdin=subprocess.DEVNULL,
                           creationflags=_NO_WINDOW)
            self._emit("toast", text="Shortcuts added to the Start menu and desktop. If it "
                                     "didn't pin itself, right-click the Start menu entry "
                                     "and choose Pin to taskbar.")
        except Exception as e:  # noqa: BLE001
            self._emit("toast", text=f"Could not create shortcuts: {e}")


# --------------------------------------------------------------------------- #
#  Self-test and start-up                                                      #
# --------------------------------------------------------------------------- #
def selftest() -> None:
    """Verify the frozen bundle can load its heavy lazy dependencies. Writes the
    result to selftest_result.txt next to the exe (works even when windowed)."""
    lines = []
    ok = True
    for mod in ("playwright.sync_api", "googleapiclient.discovery",
                "google_auth_oauthlib.flow", "google.oauth2.credentials",
                "google.genai", "anthropic", "truststore", "requests", "webview",
                "clr_loader"):
        try:
            __import__(mod)
            lines.append(f"OK   import {mod}")
        except Exception as e:
            ok = False
            lines.append(f"FAIL import {mod}: {e}")
    ui = resource_dir() / "ui" / "index.html"
    lines.append(("OK   " if ui.exists() else "FAIL ") + f"page bundled: {ui.name}")
    ok = ok and ui.exists()
    try:
        eng = AgentEngine(AgentConfig(anthropic_api_key="sk-test",
                                      gmail_enabled=True, gdrive_enabled=True,
                                      github_enabled=True), DATA_DIR)
        lines.append(f"OK   engine built, {len(eng.tools)} tools")
        # Verify subprocess tools work in the frozen (no-console) context.
        out = eng.t_run_cmd("echo FROZEN_CMD_OK")
        lines.append(("OK   " if "FROZEN_CMD_OK" in out else "FAIL ") + f"run_cmd -> {out.strip()[:40]}")
        if "FROZEN_CMD_OK" not in out:
            ok = False
        pout = eng.t_run_powershell("Write-Output FROZEN_PS_OK")
        lines.append(("OK   " if "FROZEN_PS_OK" in pout else "FAIL ") + f"run_powershell -> {pout.strip()[:40]}")
        if "FROZEN_PS_OK" not in pout:
            ok = False
        # Detached-launch must NOT deadlock: a grandchild that outlives the
        # call (here a ~20s ping) used to hold the capture pipe open forever.
        t0 = time.monotonic()
        eng.t_run_cmd('start "" cmd /c ping -n 20 127.0.0.1')
        el = time.monotonic() - t0
        lines.append(("OK   " if el < 10 else "FAIL ") + f"detached-launch returned in {el:.1f}s (no deadlock)")
        if el >= 10:
            ok = False
    except Exception as e:
        ok = False
        lines.append(f"FAIL engine build: {e}")
    lines.insert(0, "SELFTEST " + ("PASS" if ok else "FAIL"))
    (base_dir() / "selftest_result.txt").write_text("\n".join(lines), encoding="utf-8")


def _no_webview2(err) -> None:
    """Edge WebView2 is missing (Windows 11 always has it; an old Windows 10 may
    not). Say so plainly and offer the download instead of failing silently."""
    import webbrowser
    from tkinter import Tk, messagebox
    root = Tk()
    root.withdraw()
    if messagebox.askyesno(
            APP_TITLE,
            f"{APP_TITLE} needs Microsoft Edge WebView2 to show its window, and this "
            f"computer does not have it ({err}).\n\nOpen the free download from "
            "Microsoft now? After installing it, start the program again."):
        webbrowser.open(WEBVIEW2_URL)
    root.destroy()


def main():
    if "--selftest" in sys.argv:
        selftest()
        return
    if "--update-notify" in sys.argv:
        update_notify()
        return
    import webview
    api = Api()
    page = resource_dir() / "ui" / "index.html"
    window = webview.create_window(
        APP_TITLE, url=str(page), js_api=api, width=1160, height=800,
        min_size=(900, 620), background_color="#F7F9FD", text_select=True)
    api._window = window
    window.events.closing += api._on_closing
    threading.Thread(target=register_update_task, daemon=True).start()
    try:
        webview.start(gui="edgechromium", private_mode=True)
    except Exception as e:  # noqa: BLE001
        _no_webview2(e)


if __name__ == "__main__":
    main()
