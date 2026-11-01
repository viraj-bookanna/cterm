"""Inject a self-keep-alive daemon into the Colab runtime.

Mirrors the structure of ``drive.py``: upload a static agent script and a
per-session config file via the Jupyter contents API, then launch the daemon
through a short-lived ``setsid`` terminal so it survives the terminal close.

The daemon (``_agent/keepalive.py``) reads its JSON config, immediately
unlinks it to limit on-disk exposure of OAuth tokens, then pings the
keep-alive endpoint every ``INTERVAL`` seconds, refreshing the access token
automatically before it expires.

A single successful call is enough: injection waits for the daemon's PID
file to appear before returning. If an existing daemon is already running
on the VM, we skip injection and reuse it (no duplicate pingers).

Public entry point
------------------
    from .keepalive import inject_self_keep_alive
    ok = inject_self_keep_alive(client, server_id)
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from .client import ColabClient
from .constants import (
    APP_NAME,
    CLIENT_ID,
    CLIENT_SECRET,
    EXTENSION_VERSION,
    KEEP_ALIVE_INTERVAL,
)
from .tunnel import run_script_in_terminal, upload_file
from .utils import err, log

# Remote paths used on the Colab VM.
_AGENT_REMOTE = "/content/_cterm_keepalive.py"
_CONFIG_REMOTE = "/tmp/.cterm_keepalive.json"
_PID_REMOTE = "/tmp/.cterm_keepalive.pid"
_LOG_REMOTE = "/tmp/.cterm_keepalive.log"
_STOP_REMOTE = "/tmp/.cterm_keepalive.stop"

_TOKEN_URL = "https://oauth2.googleapis.com/token"

# Primary launch command: setsid detaches from the controlling terminal,
# nohup ignores SIGHUP, redirects move FDs off the terminal before Jupyter
# tears it down. The trailing `&` + `disown` keeps bash from waiting.
_LAUNCH_CMD = (
    f"rm -f {_STOP_REMOTE}; "
    f"nohup setsid python3 {_AGENT_REMOTE} {_CONFIG_REMOTE}"
    f" </dev/null >{_LOG_REMOTE} 2>&1 &"
    f" disown $! 2>/dev/null; true"
)

# How long to wait for the daemon's PID file to appear after launch.
_PID_WAIT_SECONDS = 15.0
_PID_POLL_INTERVAL = 0.5


def _pid_file_contents(client: ColabClient) -> str | None:
    """Return the PID file contents if it exists on the VM, else None."""
    try:
        entry = client.contents_get(_PID_REMOTE, content=True)
    except Exception:  # noqa: BLE001
        return None
    content = entry.get("content", "") or ""
    return content.strip() or None


def _wait_for_pid_file(client: ColabClient, timeout_s: float) -> str | None:
    """Poll the Jupyter contents API for the daemon's PID file."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        pid = _pid_file_contents(client)
        if pid:
            return pid
        time.sleep(_PID_POLL_INTERVAL)
    return None


def inject_self_keep_alive(client: ColabClient, server_id: str) -> bool:
    """Upload and start the keep-alive daemon on the Colab runtime.

    Returns True once the daemon is confirmed running (PID file present).
    On failure, the caller should fall back to local polling via
    ``RuntimeManager.start_keep_alive()``.

    If a prior daemon already left a PID file on the VM, this call is a
    no-op success - duplicate pingers waste quota and achieve nothing.
    """
    # Short-circuit: if a prior daemon is already running on this VM, reuse it.
    existing_pid = _pid_file_contents(client)
    if existing_pid:
        log(
            f"Self-keep-alive daemon already running on the runtime "
            f"(pid {existing_pid}); reusing it."
        )
        return True

    # 1. Read the static agent source.
    agent_src = Path(__file__).parent / "_agent" / "keepalive.py"
    try:
        agent_content = agent_src.read_text(encoding="utf-8")
    except OSError as exc:
        err(f"Cannot read keep-alive agent source: {exc}")
        return False

    # 2. Build the per-session config (contains OAuth tokens - uploaded to
    #    /tmp where only root can read it, and the daemon unlinks it on start).
    config = {
        "ACCESS_TOKEN": client.auth.access_token or "",
        "REFRESH_TOKEN": client.auth.refresh_token or "",
        "CLIENT_ID": CLIENT_ID,
        "CLIENT_SECRET": CLIENT_SECRET,
        "KA_URL": client.keep_alive_url(server_id),
        "TOKEN_URL": _TOKEN_URL,
        "INTERVAL": KEEP_ALIVE_INTERVAL,
        # Required so the on-VM pings carry the same extension-identifying
        # headers the local client sends. Without them Colab silently drops
        # the request from idle accounting.
        "APP_NAME": APP_NAME,
        "EXT_VERSION": EXTENSION_VERSION,
    }
    config_content = json.dumps(config)

    # 3. Upload the agent script (static, same across sessions).
    if not upload_file(client, _AGENT_REMOTE, agent_content, "keep-alive agent"):
        return False

    # 4. Upload the ephemeral config (contains tokens).
    if not upload_file(client, _CONFIG_REMOTE, config_content, "keep-alive config"):
        return False

    # 5. Launch the daemon via a short-lived terminal, then confirm it took.
    log("Launching self-keep-alive daemon on the runtime...")
    if not run_script_in_terminal(
        client,
        _AGENT_REMOTE,
        "keepalive",
        launch_cmd=_LAUNCH_CMD,
    ):
        return False

    pid = _wait_for_pid_file(client, _PID_WAIT_SECONDS)
    if not pid:
        err(
            f"Self-keep-alive daemon did not report ready within "
            f"{_PID_WAIT_SECONDS:.0f}s (no PID file at {_PID_REMOTE}). "
            f"Check {_LOG_REMOTE} on the runtime."
        )
        return False

    log(
        f"Self-keep-alive daemon started on the runtime "
        f"(pid {pid}, logs in {_LOG_REMOTE})."
    )
    return True
