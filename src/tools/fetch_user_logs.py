r"""
Bring the logs users sent with "Send logs" down into user_logs/ as text files,
then delete each one from the server once it is safely on disk.

    ..\venv\Scripts\python.exe tools\fetch_user_logs.py          (from AI_Agent_App)

The read token is NOT in this repository. It is read from the environment
variable MYDESKAI_LOGS_TOKEN, or from the file
    %APPDATA%\MyDeskAI-dev\logs_read_token.txt
and must match the Worker secret READ_TOKEN (deploy/logs-worker).

user_logs/ is in .gitignore: users' logs never go into the public repository.
"""

import json
import os
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path

try:                      # trust the Windows certificate store (content filters)
    import truststore
    truststore.inject_into_ssl()
except Exception:  # noqa: BLE001
    pass

BASE = os.environ.get("MYDESKAI_LOGS_URL", "https://logs.kolsatmar.org")
HERE = Path(__file__).resolve().parent.parent
OUT = HERE / "user_logs"
UA = "MyDeskAI-log-fetcher/1"


def token() -> str:
    t = os.environ.get("MYDESKAI_LOGS_TOKEN", "").strip()
    if t:
        return t
    f = Path(os.environ.get("APPDATA", str(Path.home()))) / "MyDeskAI-dev" / "logs_read_token.txt"
    if f.exists():
        return f.read_text(encoding="utf-8").strip()
    sys.exit(f"No read token: set MYDESKAI_LOGS_TOKEN or create {f}")


def call(path: str, tok: str, method: str = "GET", **params) -> bytes:
    url = BASE + path + ("?" + urllib.parse.urlencode(params) if params else "")
    req = urllib.request.Request(url, method=method, data=b"" if method == "POST" else None,
                                 headers={"Authorization": f"Bearer {tok}", "User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


def main() -> int:
    tok = token()
    OUT.mkdir(exist_ok=True)
    got = 0
    cursor = None
    while True:
        params = {"cursor": cursor} if cursor else {}
        data = json.loads(call("/list", tok, **params))
        for k in data.get("keys", []):
            key = k["key"]
            name = re.sub(r"[^A-Za-z0-9_.-]", "_", key.split(":", 1)[1])
            ver = re.sub(r"[^0-9A-Za-z_.-]", "", str(k.get("version") or ""))
            path = OUT / (name + (f"_v{ver}" if ver else "") + ".txt")
            text = call("/get", tok, key=key).decode("utf-8", errors="replace")
            path.write_text(text, encoding="utf-8")
            if path.stat().st_size >= len(text.encode("utf-8")) * 0.99:   # written whole
                call("/delete", tok, method="POST", key=key)
            got += 1
            print(f"saved {path.name}  (number {k.get('id')}, {k.get('country') or '?'})")
        cursor = data.get("cursor")
        if not cursor:
            break
    print(f"{got} new log(s)" if got else "no new logs")
    return 0


if __name__ == "__main__":
    sys.exit(main())
