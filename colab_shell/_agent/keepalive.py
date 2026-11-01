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
APP_NAME       : X-Colab-VS-Code-App-Name header value (optional)
EXT_VERSION    : X-Colab-VS-Code-Extension-Version header value (optional)

Runtime behaviour
-----------------
- PID written to /tmp/.cterm_keepalive.pid so the daemon can be found.
- Touch /tmp/.cterm_keepalive.stop on the runtime to stop it gracefully.
- Logs go to /tmp/.cterm_keepalive.log (captured by the shell redirect above).
- ``cterm kill`` is still the authoritative way to terminate the runtime.
- Every ping is logged with its HTTP status so misconfigurations are visible
  (previously the daemon only sent an Authorization header; without the
  extension-identifying headers Colab silently ignores the ping and the
  runtime still idles out).
- 401 responses trigger an immediate token refresh instead of waiting for the
  conservative expiry window.
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
APP_NAME: str = _cfg.get("APP_NAME", "Cursor")
EXT_VERSION: str = _cfg.get("EXT_VERSION", "0.8.1")

# ---------------------------------------------------------------------------
# Daemon state
# ---------------------------------------------------------------------------

STOP_FILE = "/tmp/.cterm_keepalive.stop"
PID_FILE = "/tmp/.cterm_keepalive.pid"

with open(PID_FILE, "w") as _pid_f:
    _pid_f.write(str(os.getpid()))

# Track when the current access token expires (conservative: 60 s early).
_expiry = time.time() + 3500

_ping_count = 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _log(msg: str) -> None:
    print(f"[keepalive {time.strftime('%H:%M:%S')}] {msg}", flush=True)


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
    _log("access token refreshed")


def _ping_once() -> int:
    """Send one keep-alive request. Returns the HTTP status code (0 on error)."""
    req = urllib.request.Request(
        KA_URL,
        headers={
            "Authorization": f"Bearer {ACCESS_TOKEN}",
            # These headers mimic the VS Code extension. Without them the
            # Colab keep-alive endpoint accepts the request but does NOT
            # count it as user activity, so the runtime still idles out.
            "X-Colab-Client-Agent": "vscode",
            "X-Colab-VS-Code-App-Name": APP_NAME,
            "X-Colab-VS-Code-Extension-Version": EXT_VERSION,
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return int(resp.getcode() or 0)
    except urllib.error.HTTPError as e:
        return int(e.code)
    except Exception as e:  # noqa: BLE001
        _log(f"ping transport error: {e}")
        return 0


def _ping() -> None:
    global _ping_count  # noqa: PLW0603
    if time.time() > _expiry:
        try:
            _refresh()
        except Exception as e:  # noqa: BLE001
            _log(f"token refresh failed: {e}")

    code = _ping_once()
    _ping_count += 1

    if code == 401:
        # Token expired early (e.g. revoked); force a refresh and retry once.
        try:
            _refresh()
            code = _ping_once()
        except Exception as e:  # noqa: BLE001
            _log(f"force-refresh failed: {e}")

    # Log every ping for the first few, then only one in every 20 (10 min at 30 s)
    # or any non-2xx response.
    if _ping_count <= 3 or _ping_count % 20 == 0 or not (200 <= code < 300):
        _log(f"ping #{_ping_count} HTTP {code}")


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

_log(
    f"started pid={os.getpid()} interval={INTERVAL}s ka_url={KA_URL} "
    f"app_name={APP_NAME!r} ext_version={EXT_VERSION!r}"
)
try:
    while not os.path.exists(STOP_FILE):
        _ping()
        time.sleep(INTERVAL)
    _log("stop file found - exiting")
finally:
    try:
        os.unlink(PID_FILE)
    except OSError:
        pass
