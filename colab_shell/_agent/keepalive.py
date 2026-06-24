"""Self-keep-alive daemon for the Colab runtime.

Uploaded by ``cterm`` to ``/content/_cterm_keepalive.py`` and launched via a
short-lived Jupyter terminal::

    setsid python3 /content/_cterm_keepalive.py /tmp/.cterm_keepalive.json \
        </dev/null &>/tmp/.cterm_keepalive.log &

The JSON config file at the path given in sys.argv[1] is read once and then
immediately deleted to limit on-disk exposure of OAuth tokens.

Config keys
-----------
ACCESS_TOKEN   : current Google OAuth2 access token
REFRESH_TOKEN  : OAuth2 refresh token (used to renew the access token)
CLIENT_ID      : OAuth2 client id
CLIENT_SECRET  : OAuth2 client secret
KA_URL         : Colab keep-alive endpoint URL
TOKEN_URL      : Google token endpoint (https://oauth2.googleapis.com/token)
INTERVAL       : seconds between keep-alive pings (int)

Runtime behaviour
-----------------
- PID written to /tmp/.cterm_keepalive.pid so the daemon can be found.
- Touch /tmp/.cterm_keepalive.stop on the runtime to stop it gracefully.
- Logs go to /tmp/.cterm_keepalive.log (captured by the shell redirect above).
- ``cterm kill`` is still the authoritative way to terminate the runtime.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.parse
import urllib.request

# ---------------------------------------------------------------------------
# Load config and erase it immediately
# ---------------------------------------------------------------------------

if len(sys.argv) < 2:
    sys.exit("usage: keepalive.py <config-json-path>")

_cfg_path = sys.argv[1]
try:
    with open(_cfg_path, encoding="utf-8") as _f:
        _cfg = json.load(_f)
finally:
    try:
        os.unlink(_cfg_path)
    except OSError:
        pass

ACCESS_TOKEN: str = _cfg["ACCESS_TOKEN"]
REFRESH_TOKEN: str = _cfg["REFRESH_TOKEN"]
CLIENT_ID: str = _cfg["CLIENT_ID"]
CLIENT_SECRET: str = _cfg["CLIENT_SECRET"]
KA_URL: str = _cfg["KA_URL"]
TOKEN_URL: str = _cfg["TOKEN_URL"]
INTERVAL: int = int(_cfg["INTERVAL"])

# ---------------------------------------------------------------------------
# Daemon state
# ---------------------------------------------------------------------------

STOP_FILE = "/tmp/.cterm_keepalive.stop"
PID_FILE = "/tmp/.cterm_keepalive.pid"

with open(PID_FILE, "w") as _pid_f:
    _pid_f.write(str(os.getpid()))

# Track when the current access token expires (conservative: 60 s early).
_expiry = time.time() + 3500


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _refresh() -> None:
    global ACCESS_TOKEN, _expiry  # noqa: PLW0603
    data = urllib.parse.urlencode(
        {
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "refresh_token": REFRESH_TOKEN,
            "grant_type": "refresh_token",
        }
    ).encode()
    req = urllib.request.Request(TOKEN_URL, data)
    with urllib.request.urlopen(req, timeout=15) as resp:
        d = json.loads(resp.read())
    ACCESS_TOKEN = d["access_token"]
    _expiry = time.time() + d.get("expires_in", 3600) - 60


def _ping() -> None:
    if time.time() > _expiry:
        try:
            _refresh()
        except Exception:  # noqa: BLE001
            pass
    req = urllib.request.Request(
        KA_URL, headers={"Authorization": f"Bearer {ACCESS_TOKEN}"}
    )
    try:
        urllib.request.urlopen(req, timeout=10).close()
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

try:
    while not os.path.exists(STOP_FILE):
        _ping()
        time.sleep(INTERVAL)
finally:
    try:
        os.unlink(PID_FILE)
    except OSError:
        pass
