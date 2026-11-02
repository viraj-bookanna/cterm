"""Self-keep-alive daemon for the Colab runtime.

Uploaded by ``cterm`` to ``/content/_cterm_keepalive.py`` and launched via a
short-lived Jupyter terminal::

    setsid python3 /content/_cterm_keepalive.py /tmp/.cterm_keepalive.json \
        </dev/null &>/tmp/.cterm_keepalive.log &

The JSON config file at the path given in sys.argv[1] is read once and then
immediately deleted to limit on-disk exposure of OAuth tokens.

Config keys
-----------
ACCESS_TOKEN            : current Google OAuth2 access token
REFRESH_TOKEN           : OAuth2 refresh token
CLIENT_ID / SECRET      : OAuth2 client credentials
SERVER_ID               : Colab runtime endpoint id
KA_URL                  : keep-alive endpoint URL
PROXY_TOKEN_URL         : COLAB_GAPI/v1/runtime-proxy-token
ASSIGNMENTS_URL         : COLAB_GAPI/v1/assignments
TOKEN_URL               : https://oauth2.googleapis.com/token
INTERVAL                : baseline seconds between keep-alive pings (jittered)
PROXY_REFRESH_INTERVAL  : baseline seconds between proxy-token refreshes
ASSIGNMENTS_INTERVAL    : baseline seconds between assignments pings
APP_NAME / EXT_VERSION  : extension-identifying header values

Behaviour notes
---------------
- Keep-alive pings run on a jittered sub-interval ``[0.5x, 1.5x]`` of
  INTERVAL so the pattern isn't a predictable 30s heartbeat Colab can
  filter.
- The proxy-token endpoint is refreshed every ~PROXY_REFRESH_INTERVAL
  seconds (also jittered). Each refresh counts as editor activity.
- The assignments list is fetched every ~ASSIGNMENTS_INTERVAL seconds
  (also jittered) - another editor-like activity signal.
- 401 on any call triggers an immediate OAuth refresh and retry.
- Touch /tmp/.cterm_keepalive.stop on the VM to stop the daemon.
- Logs: /tmp/.cterm_keepalive.log
"""

from __future__ import annotations

import json
import os
import random
import sys
import time
import urllib.error
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
SERVER_ID: str = _cfg.get("SERVER_ID", "")
KA_URL: str = _cfg["KA_URL"]
PROXY_TOKEN_URL: str = _cfg.get("PROXY_TOKEN_URL", "")
ASSIGNMENTS_URL: str = _cfg.get("ASSIGNMENTS_URL", "")
TOKEN_URL: str = _cfg["TOKEN_URL"]
INTERVAL: float = float(_cfg["INTERVAL"])
PROXY_REFRESH_INTERVAL: float = float(_cfg.get("PROXY_REFRESH_INTERVAL", 120))
ASSIGNMENTS_INTERVAL: float = float(_cfg.get("ASSIGNMENTS_INTERVAL", 180))
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

# Next-fire timestamps for the two slower activity signals.
_next_proxy_refresh = 0.0        # fire immediately on first tick
_next_assignments = 0.0          # fire immediately on first tick

# Per-type counters so we can log occasional progress without spamming.
_counts: dict[str, int] = {"ka": 0, "proxy": 0, "assign": 0}

# XSS prefix that COLAB_GAPI prepends to JSON responses.
_XSS_PREFIX = b")]}'\n"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _log(msg: str) -> None:
    print(f"[keepalive {time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _ext_headers() -> dict[str, str]:
    """Headers the Colab VS Code extension sends on every request."""
    return {
        "Authorization": f"Bearer {ACCESS_TOKEN}",
        "X-Colab-Client-Agent": "vscode",
        "X-Colab-VS-Code-App-Name": APP_NAME,
        "X-Colab-VS-Code-Extension-Version": EXT_VERSION,
        "Content-Type": "application/json",
    }


def _refresh_oauth() -> None:
    """Refresh the OAuth access token."""
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
    _log("oauth access token refreshed")


def _call(url: str, *, method: str = "GET", timeout: int = 10) -> int:
    """Fire one HTTP request with extension headers; return status code."""
    req = urllib.request.Request(url, headers=_ext_headers(), method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            # Drain so the connection can be reused.
            resp.read(256)
            return int(resp.getcode() or 0)
    except urllib.error.HTTPError as e:
        return int(e.code)
    except Exception as e:  # noqa: BLE001
        _log(f"transport error on {url.split('?')[0]}: {e}")
        return 0


def _with_auth_retry(fn):
    """Run ``fn()`` once; if it returns 401, refresh OAuth and run again."""
    code = fn()
    if code == 401:
        try:
            _refresh_oauth()
            code = fn()
        except Exception as e:  # noqa: BLE001
            _log(f"forced oauth refresh failed: {e}")
    return code


def _ping_keep_alive() -> int:
    _counts["ka"] += 1
    return _with_auth_retry(lambda: _call(KA_URL))


def _ping_proxy_token() -> int:
    """Refresh the runtime proxy token. Doubles as an activity signal."""
    if not PROXY_TOKEN_URL or not SERVER_ID:
        return 0
    _counts["proxy"] += 1
    url = (
        f"{PROXY_TOKEN_URL}?"
        f"endpoint={urllib.parse.quote(SERVER_ID, safe='')}&port=8080"
    )
    return _with_auth_retry(lambda: _call(url))


def _ping_assignments() -> int:
    """List assignments - another request shape the extension makes."""
    if not ASSIGNMENTS_URL:
        return 0
    _counts["assign"] += 1
    return _with_auth_retry(lambda: _call(ASSIGNMENTS_URL))


def _jitter(base: float, lo: float = 0.5, hi: float = 1.5) -> float:
    """Return a random float in ``[base*lo, base*hi]`` with 1s floor."""
    return max(1.0, random.uniform(base * lo, base * hi))


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

_log(
    f"started pid={os.getpid()} base_interval={INTERVAL}s "
    f"proxy_refresh={PROXY_REFRESH_INTERVAL}s "
    f"assign_ping={ASSIGNMENTS_INTERVAL}s "
    f"server_id={SERVER_ID[:12] + '...' if SERVER_ID else '(none)'}"
)

try:
    while not os.path.exists(STOP_FILE):
        now = time.time()

        # Refresh OAuth proactively a bit before expiry.
        if now > _expiry:
            try:
                _refresh_oauth()
            except Exception as e:  # noqa: BLE001
                _log(f"oauth refresh failed: {e}")

        # 1. Primary keep-alive ping (every jittered sub-interval).
        ka_code = _ping_keep_alive()
        if _counts["ka"] <= 3 or _counts["ka"] % 20 == 0 or not (200 <= ka_code < 300):
            _log(f"keep-alive #{_counts['ka']} HTTP {ka_code}")

        # 2. Proxy-token refresh (every ~PROXY_REFRESH_INTERVAL).
        if now >= _next_proxy_refresh:
            px_code = _ping_proxy_token()
            _log(f"proxy-token #{_counts['proxy']} HTTP {px_code}")
            _next_proxy_refresh = now + _jitter(PROXY_REFRESH_INTERVAL, 0.7, 1.3)

        # 3. Assignments list (every ~ASSIGNMENTS_INTERVAL).
        if now >= _next_assignments:
            as_code = _ping_assignments()
            _log(f"assignments #{_counts['assign']} HTTP {as_code}")
            _next_assignments = now + _jitter(ASSIGNMENTS_INTERVAL, 0.7, 1.3)

        # Jittered sleep for the next keep-alive tick.
        sleep_for = _jitter(INTERVAL)
        # Interruptible sleep: wake every second so STOP_FILE is honored fast.
        slept = 0.0
        while slept < sleep_for and not os.path.exists(STOP_FILE):
            step = min(1.0, sleep_for - slept)
            time.sleep(step)
            slept += step

    _log("stop file found - exiting")
finally:
    try:
        os.unlink(PID_FILE)
    except OSError:
        pass
