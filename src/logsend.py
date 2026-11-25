"""
"Send logs": gather the app's activity log, strip every secret out of it, and
send it to the developer so a problem a user hits can be seen and fixed.

What is sent, and only after the user has seen it in the Send logs window:
  - a short header: app version, Windows version, which engine and model, which
    connectors are on (yes/no only - never a key, token or password);
  - the user's own note, if they wrote one;
  - the end of agent.log (and agent.old.log), at most MAX_LOG_CHARS.

Every secret the settings hold is replaced by [REDACTED] wherever it appears,
and so is anything that merely looks like a key or token (redact()).

The upload is a gzip, base64-encoded inside JSON. Some content filters read
request bodies and block a request because of words in it; an opaque body
cannot trip that. The server (deploy/logs-worker) answers {"ok": true, "id"};
anything else - a filter's block page, a sign-in page - counts as a failure,
and the app then saves the same report on the Desktop instead.
"""

import base64
import gzip
import json
import os
import platform
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Dict, Iterable, Optional

UPLOAD_URL = "https://logs.kolsatmar.org/upload"
MAX_LOG_CHARS = 400_000
MAX_NOTE_CHARS = 4000
REDACTED = "[REDACTED]"

# The settings keys whose VALUES are secret.
SECRET_KEYS = ("anthropic_api_key", "gemini_api_key", "github_token",
               "custom_auth_header")

# Shapes of secrets that may turn up in a log even though they are not in the
# settings (a key pasted into the chat, a token in an error message).
_PATTERNS = [
    (re.compile(r"sk-ant-[A-Za-z0-9_\-]{10,}"), REDACTED),
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}"), REDACTED),
    (re.compile(r"AIza[0-9A-Za-z_\-]{30,}"), REDACTED),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"), REDACTED),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"), REDACTED),
    (re.compile(r"\bya29\.[0-9A-Za-z_\-.]{10,}"), REDACTED),
    (re.compile(r"\b1//0[0-9A-Za-z_\-]{20,}"), REDACTED),
    (re.compile(r"\bGOCSPX-[0-9A-Za-z_\-]{10,}"), REDACTED),
    (re.compile(r"\bxox[abposr]-[0-9A-Za-z\-]{10,}"), REDACTED),
    (re.compile(r"(?i)(\bbearer\s+)[A-Za-z0-9\-._~+/]{12,}=*"), r"\1" + REDACTED),
    (re.compile(r"(?i)(\b(?:x-api-key|authorization)\s*[:=]\s*)\S+"), r"\1" + REDACTED),
    (re.compile(r"(?i)(\"?\b(?:password|passwd|pwd|secret|client_secret|api[_-]?key|"
                r"access_token|refresh_token|token)\b\"?\s*[:=]\s*\"?)[^\"\s,;}]{4,}"),
     r"\1" + REDACTED),
]


def redact(text: str, secrets: Iterable[str] = ()) -> str:
    """Remove every known secret value, then anything shaped like a secret."""
    text = text or ""
    # Longest first, so a value that contains another one is removed whole.
    for s in sorted({(s or "").strip() for s in secrets}, key=len, reverse=True):
        if len(s) >= 6:
            text = text.replace(s, REDACTED)
            # "Authorization: Bearer abc..." is stored whole; its token alone may
            # also appear in a log line.
            tail = s.split()[-1]
            if len(tail) >= 12 and tail != s:
                text = text.replace(tail, REDACTED)
    for rx, rep in _PATTERNS:
        text = rx.sub(rep, text)
    return text


def secrets_from(values: Dict) -> list:
    return [str(values.get(k) or "") for k in SECRET_KEYS]


def _tail(path: Path, limit: int) -> str:
    try:
        size = path.stat().st_size
    except OSError:
        return ""
    with open(path, "rb") as f:
        if size > limit * 4:               # UTF-8 Yiddish is ~2 bytes a letter
            f.seek(size - limit * 4)
        data = f.read().decode("utf-8", errors="replace")
    if len(data) > limit:
        data = data[-limit:]
        data = data[data.find("\n") + 1:]  # start on a whole line
    return data


def read_log(data_dir: Path, limit: int = MAX_LOG_CHARS) -> str:
    """The newest `limit` characters of the log, older file first."""
    new = _tail(Path(data_dir) / "agent.log", limit)
    old = ""
    if len(new) < limit:
        old = _tail(Path(data_dir) / "agent.old.log", limit - len(new))
    if old and new:
        return old.rstrip("\n") + "\n" + new
    return old or new


def header(values: Dict, app_title: str, version: str, frozen: bool) -> str:
    on = lambda k: "on" if values.get(k) else "off"        # noqa: E731
    backend = "Claude subscription (Claude Code CLI)" \
        if values.get("ai_backend") == "claude_code" else "Claude API"
    model = values.get("claude_code_model") if values.get("ai_backend") == "claude_code" \
        else values.get("model")
    lines = [
        f"{app_title} v{version} ({'exe' if frozen else 'source'})",
        f"Sent: {time.strftime('%Y-%m-%d %H:%M:%S')} (UTC{time.strftime('%z')})",
        f"Windows: {platform.platform()}",
        f"Engine: {backend}, model {model or '(default)'}, effort {values.get('effort') or '(default)'}",
        "Keys set: Anthropic " + ("yes" if (values.get('anthropic_api_key') or '').strip() else "no")
        + ", Gemini " + ("yes" if (values.get('gemini_api_key') or '').strip() else "no"),
        f"Access: {values.get('access_mode') or 'full'}, autonomous {on('autonomous')}",
        f"Connectors: Gmail {on('gmail_enabled')}, Drive {on('gdrive_enabled')}, "
        f"SMS {on('sms_enabled')}, GitHub {on('github_enabled')}, Web API {on('custom_enabled')}",
    ]
    return "\n".join(lines)


def build_report(data_dir: Path, values: Dict, app_title: str, version: str,
                 frozen: bool, note: str = "") -> str:
    note = (note or "").strip()[:MAX_NOTE_CHARS]
    parts = [header(values, app_title, version, frozen)]
    parts.append("What the user says happened:\n" + (note or "(nothing written)"))
    log = read_log(data_dir)
    parts.append("Activity log (newest at the bottom):\n" + (log or "(the log is empty)"))
    sep = "\n" + "-" * 60 + "\n"
    return redact(sep.join(p.rstrip() for p in parts) + "\n", secrets_from(values))


class UploadFailed(Exception):
    pass


def upload(report: str, app_title: str, version: str,
           url: str = UPLOAD_URL, timeout: float = 30) -> str:
    """Send the report; returns the id the server gave it."""
    gz = gzip.compress(report.encode("utf-8"))
    body = json.dumps({"app": app_title, "version": version,
                       "gz": base64.b64encode(gz).decode("ascii")}).encode("ascii")
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/json",
                 "User-Agent": f"{app_title}/{version}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read(65536)
    except urllib.error.HTTPError as e:
        raw = e.read(65536)
        try:
            msg = json.loads(raw).get("error") or f"HTTP {e.code}"
        except Exception:  # noqa: BLE001
            msg = f"HTTP {e.code}"
        raise UploadFailed(msg) from None
    except Exception as e:  # noqa: BLE001 - offline, DNS, certificate
        raise UploadFailed(f"{type(e).__name__}: {e}") from None
    try:
        data = json.loads(raw)
    except Exception:  # noqa: BLE001
        # HTTP 200 with a web page: a filter or a proxy answered, not our server.
        raise UploadFailed("the answer did not come from the log server "
                           "(an internet filter may have stopped it)") from None
    if not (isinstance(data, dict) and data.get("ok") and data.get("id")):
        raise UploadFailed(str(data.get("error") if isinstance(data, dict) else data))
    return str(data["id"])


def desktop() -> Path:
    """The user's real Desktop folder (OneDrive moves it on many PCs)."""
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes
            buf = ctypes.create_unicode_buffer(wintypes.MAX_PATH)
            # CSIDL_DESKTOPDIRECTORY = 0x10
            if ctypes.windll.shell32.SHGetFolderPathW(None, 0x10, None, 0, buf) == 0 and buf.value:
                return Path(buf.value)
        except Exception:  # noqa: BLE001
            pass
    for p in (Path.home() / "Desktop", Path.home() / "OneDrive" / "Desktop"):
        if p.is_dir():
            return p
    return Path.home()


def save_copy(report: str, app_title: str, folder: Optional[Path] = None) -> Path:
    folder = Path(folder or desktop())
    path = folder / f"{app_title}-logs-{time.strftime('%Y%m%d-%H%M%S')}.txt"
    path.write_text(report, encoding="utf-8")
    return path
