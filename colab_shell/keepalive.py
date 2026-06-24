"""Inject a self-keep-alive daemon into the Colab runtime.

Mirrors the structure of ``drive.py``: upload a static agent script and a
per-session config file via the Jupyter contents API, then launch the daemon
through a short-lived ``setsid`` terminal so it survives the terminal close.

The daemon (``_agent/keepalive.py``) reads its JSON config, immediately
unlinks it to limit on-disk exposure of OAuth tokens, then pings the
keep-alive endpoint every ``INTERVAL`` seconds, refreshing the access token
automatically before it expires.

Public entry point
------------------
    from .keepalive import inject_self_keep_alive
    ok = inject_self_keep_alive(client, server_id)
"""

from __future__ import annotations

import json
from pathlib import Path

from .client import ColabClient
from .constants import CLIENT_ID, CLIENT_SECRET, KEEP_ALIVE_INTERVAL
from .tunnel import run_script_in_terminal, upload_file
from .utils import err, log

# Remote paths used on the Colab VM.
_AGENT_REMOTE = "/content/_cterm_keepalive.py"
_CONFIG_REMOTE = "/tmp/.cterm_keepalive.json"

_TOKEN_URL = "https://oauth2.googleapis.com/token"

# The launch command: run with setsid so the process survives the terminal
# close; redirect stdout/stderr to the log file.
_LAUNCH_CMD = (
    f"setsid python3 {_AGENT_REMOTE} {_CONFIG_REMOTE}"
    f" </dev/null &>/tmp/.cterm_keepalive.log &"
)


def inject_self_keep_alive(client: ColabClient, server_id: str) -> bool:
    """Upload and start the keep-alive daemon on the Colab runtime.

    Returns True on successful dispatch, False on any upload or launch error.
    On failure the caller should fall back to local polling via
    ``RuntimeManager.start_keep_alive()``.
    """
    # 1. Read the static agent source.
    agent_src = Path(__file__).parent / "_agent" / "keepalive.py"
    try:
        agent_content = agent_src.read_text(encoding="utf-8")
    except OSError as exc:
        err(f"Cannot read keep-alive agent source: {exc}")
        return False

    # 2. Build the per-session config (contains OAuth tokens — uploaded to
    #    /tmp where only root can read it, and the daemon unlinks it on start).
    config = {
        "ACCESS_TOKEN": client.auth.access_token or "",
        "REFRESH_TOKEN": client.auth.refresh_token or "",
        "CLIENT_ID": CLIENT_ID,
        "CLIENT_SECRET": CLIENT_SECRET,
        "KA_URL": client.keep_alive_url(server_id),
        "TOKEN_URL": _TOKEN_URL,
        "INTERVAL": KEEP_ALIVE_INTERVAL,
    }
    config_content = json.dumps(config)

    # 3. Upload the agent script (static, same across sessions).
    if not upload_file(client, _AGENT_REMOTE, agent_content, "keep-alive agent"):
        return False

    # 4. Upload the ephemeral config (contains tokens).
    if not upload_file(client, _CONFIG_REMOTE, config_content, "keep-alive config"):
        return False

    # 5. Launch the daemon via a short-lived terminal.
    log("Launching self-keep-alive daemon on the runtime...")
    if not run_script_in_terminal(
        client,
        _AGENT_REMOTE,
        "keepalive",
        launch_cmd=_LAUNCH_CMD,
    ):
        return False

    log(
        "Self-keep-alive daemon started on the runtime "
        "(PID in /tmp/.cterm_keepalive.pid, logs in /tmp/.cterm_keepalive.log)."
    )
    return True
