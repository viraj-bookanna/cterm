"""Headless Google Drive mounting for the Colab runtime.

Mirrors the Colab extension's ephemeral-auth flow (``propagateCredentials``
with ``authType="dfs_ephemeral"``).  When credentials are already authorised
the mount completes without any browser interaction.  When they are not, the
user is directed to a Google authorisation URL and prompted to press Enter
once the browser step is done — then the mount proceeds.

The actual mount is performed by running the ``/opt/google/drive/drive`` FUSE
binary inside a Jupyter terminal, exactly as ``google.colab.drive.mount`` does
internally.  No IPython kernel is required.
"""

from __future__ import annotations

import webbrowser

import requests

from .client import ColabClient
from .utils import err, log

_AUTH_TYPE = "dfs_ephemeral"

# Remote path for the uploaded mount script.
_MOUNT_SCRIPT_REMOTE = "/content/_cterm_drive_mount.sh"

# Sentinel printed by the script so run_terminal_capture can parse the result.
_RESULT_MARKER = "__CTERM_DRIVE_RESULT__"

# Generous timeout: drive binary polls for up to 60 s, plus upload + margin.
_MOUNT_TIMEOUT = 90.0

# Bash script that replicates google.colab.drive._mount for a hosted runtime.
# It reads $TBE_EPHEM_CREDS_ADDR (already populated by _propagate via the
# Colab REST API) and launches the drive FUSE binary with identical flags.
# Emits  "__CTERM_DRIVE_RESULT__ <TOKEN>"  so the caller can parse the result.
_MOUNT_SCRIPT = """\
#!/usr/bin/env bash
MOUNTPOINT="/content/drive"
MARKER="__CTERM_DRIVE_RESULT__"
LOG=/tmp/cterm_drive.log

# Remove this script from the VM once it finishes (success or failure).
trap 'rm -f "$0"' EXIT

# Require TBE_EPHEM_CREDS_ADDR — set by the Colab backend after propagation.
META="${TBE_EPHEM_CREDS_ADDR:-}"
if [ -z "$META" ]; then
    echo "$MARKER NOENV"
    exit 0
fi

# Compute drive-binary directory the same way google.colab.drive._env() does.
ROOT_DIR=$(realpath "${CLOUDSDK_CONFIG}/../..")
DRIVE_DIR="$ROOT_DIR/opt/google/drive"
DRIVE_BIN="$DRIVE_DIR/drive"

if [ ! -x "$DRIVE_BIN" ]; then
    echo "$MARKER NOBIN"
    exit 0
fi

# Already mounted — report success immediately.
if [ -d "$MOUNTPOINT/My Drive" ] && [ -n "$(ls -A "$MOUNTPOINT" 2>/dev/null)" ]; then
    echo "$MARKER OK"
    exit 0
fi

# Clean up any stale mount or process.
umount -f "$MOUNTPOINT" 2>/dev/null || true
umount "$MOUNTPOINT" 2>/dev/null || true
pkill -9 -x drive 2>/dev/null || true
sleep 1

mkdir -p "$MOUNTPOINT"

export FUSE_DEV_NAME=/dev/fuse

# Launch drive FUSE binary detached (identical flags to google.colab.drive).
setsid "$DRIVE_BIN" \\
    --features=crash_throttle_percentage:100,fuse_max_background:1000,max_read_qps:1000,max_write_qps:1000,max_operation_batch_size:15,max_parallel_push_task_instances:10,opendir_timeout_ms:120000,virtual_folders_omit_spaces:true,read_only_mode:false \\
    --metadata_server_auth_uri="$META/computeMetadata/v1" \\
    --preferences="trusted_root_certs_file_path:$DRIVE_DIR/roots.pem,feature_flag_restart_seconds:129600,mount_point_path:$MOUNTPOINT" \\
    </dev/null >"$LOG" 2>&1 &
disown $!

# Poll for mount success (up to 60 s, same as drive.mount default).
i=0
while [ "$i" -lt 60 ]; do
    if [ -d "$MOUNTPOINT/My Drive" ] && [ -n "$(ls -A "$MOUNTPOINT" 2>/dev/null)" ]; then
        echo "$MARKER OK"
        exit 0
    fi
    sleep 1
    i=$((i + 1))
done

# Timeout — report with a snippet of the drive log.
if grep -q "domain policy has disabled Drive File Stream" "$LOG" 2>/dev/null; then
    echo "$MARKER DOMAIN_DISABLED"
else
    echo "drive log: $(tail -5 "$LOG" 2>/dev/null)"
    echo "$MARKER TIMEOUT"
fi
"""


def _propagate(client: ColabClient, server_id: str) -> bool:
    """Run the dfs_ephemeral credential-propagation flow.

    If authorisation is needed, a browser window is opened and the user must
    click Grant, then press Enter here.  Returns True on success.
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


def mount_drive(client: ColabClient, server_id: str) -> bool:
    """Propagate Drive credentials and mount Google Drive at /content/drive.

    Flow:
      1. ``_propagate`` — calls the ``dfs_ephemeral`` credentials-propagation
         API endpoint (browser-grant step, once per session).
      2. Upload a bash script to the VM that launches the ``drive`` FUSE binary
         with the same flags ``google.colab.drive.mount`` uses internally.
      3. Run the script in a Jupyter terminal and wait for the result marker.

    Returns True when ``/content/drive`` is mounted, False on any failure.
    """
    if not _propagate(client, server_id):
        return False

    from .tunnel import run_terminal_capture, upload_file

    if not upload_file(client, _MOUNT_SCRIPT_REMOTE, _MOUNT_SCRIPT, "Drive mount script"):
        return False

    log("Mounting Google Drive via terminal (this may take ~30 s)...")
    token, output = run_terminal_capture(
        client,
        f"bash {_MOUNT_SCRIPT_REMOTE}",
        _RESULT_MARKER,
        timeout=_MOUNT_TIMEOUT,
    )

    if token == "OK":
        log("Google Drive mounted at /content/drive.")
        return True

    if token == "NOENV":
        err(
            "Drive mount failed: $TBE_EPHEM_CREDS_ADDR is not set in the "
            "terminal environment. The runtime may not support ephemeral Drive auth."
        )
    elif token == "NOBIN":
        err(
            "Drive mount failed: the drive FUSE binary was not found at "
            "$CLOUDSDK_CONFIG/../../opt/google/drive/drive."
        )
    elif token == "DOMAIN_DISABLED":
        err(
            "Drive mount failed: your domain policy has disabled Drive File Stream. "
            "See https://support.google.com/a/answer/7496409"
        )
    elif token == "TIMEOUT":
        err("Drive mount timed out after 60 s. Check /tmp/cterm_drive.log on the VM.")
        if output:
            log(f"Terminal output snippet:\n{output[-500:]}")
    else:
        err(f"Drive mount did not complete (result={token!r}).")
        if output:
            log(f"Terminal output snippet:\n{output[-500:]}")

    return False
