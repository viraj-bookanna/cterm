"""Headless Google Drive mounting for the Colab runtime.

Mirrors the Colab extension's ephemeral-auth flow (``propagateCredentials``
with ``authType="dfs_ephemeral"``).  When credentials are already authorised
the mount completes without any browser interaction.  When they are not, the
user is directed to a Google authorisation URL and prompted to press Enter
once the browser step is done — then the mount proceeds.

Usage:
    from .drive import mount_drive
    mount_drive(client, server_id)  # blocks until mount is confirmed
"""

from __future__ import annotations

import webbrowser

import requests

from .client import ColabClient
from .utils import err, log

_AUTH_TYPE = "dfs_ephemeral"
_MOUNT_CMD = "python3 -c \"from google.colab import drive; drive.mount('/content/drive')\"\n"


def mount_drive(client: ColabClient, server_id: str) -> bool:
    """Propagate Drive credentials to the runtime.

    Returns True on success, False on failure.
    """
    log("Requesting Google Drive credentials propagation (dry run)...")
    try:
        result = client.propagate_credentials(server_id, _AUTH_TYPE, dry_run=True)
    except requests.RequestException as exc:
        err(f"Credential propagation dry-run failed: {exc}")
        return False

    if not result.get("success"):
        redirect_uri = result.get("unauthorizedRedirectUri")
        if not redirect_uri:
            err("Credential propagation failed with no redirect URI.")
            return False

        print()
        print("[*] Google Drive authorization required.")
        print("    Opening your browser — please complete the authorization, then")
        print("    come back here and press Enter to continue.")
        print()
        webbrowser.open(redirect_uri)
        try:
            input("    [Press Enter once you have authorized in the browser] ")
        except (EOFError, KeyboardInterrupt):
            err("Authorization cancelled.")
            return False

    # Perform the live propagation (both branches converge here).
    try:
        live = client.propagate_credentials(server_id, _AUTH_TYPE, dry_run=False)
    except requests.RequestException as exc:
        err(f"Credential propagation failed: {exc}")
        return False

    if not live.get("success"):
        err("Credential propagation was not successful.")
        return False

    log("Drive credentials propagated successfully.")
    return True


def get_mount_command() -> str:
    """Return the shell command string to mount Google Drive on the runtime."""
    return _MOUNT_CMD
