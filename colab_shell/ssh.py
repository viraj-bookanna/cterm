"""[EXPERIMENTAL] SSH shell via Colab's muxed terminal WebSocket.

Architecture
------------

  Local machine                       Colab VM (Ubuntu)
  ────────────────                    ─────────────────────────────────────
  ssh client  →  cterm listener  →  PtyTunnel (WSS)  →  pty_mux agent  →  sshd :22

Flow
----
1. Generate a throwaway Ed25519 keypair (in memory).
2. Upload and run a one-shot sshd setup script on the VM:
     - Installs openssh-server if absent (idempotent).
     - Configures key-based root login; injects the session public key.
     - Starts sshd via setsid (survives terminal close).
3. Upload the pty_mux agent (skipped if unchanged).
4. Open a PtyTunnel data channel; agent polls 127.0.0.1:22 until sshd is up.
5. Bind a local TCP listener; forward connections through the mux tunnel.
6. Auto-launch the local ``ssh`` client into an interactive shell.
7. On exit, tear down the tunnel (runtime deleted unless --keep was passed).

Notes
-----
- Passwordless sshd is intentional: the VM is temporary, deleted after use,
  and sshd is only reachable through the private tunnel on 127.0.0.1.
- base64-over-PTY overhead: fine for an interactive shell; slow for scp/rsync.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time

from .client import ColabClient
from .tunnel import PtyTunnel, handle_local_conn, require_proxy, run_script_in_terminal, upload_agent, upload_file
from .utils import err, log

# ------------------------------------------------------------------
# sshd setup script (uploaded to the VM and run once per session)
# ------------------------------------------------------------------

_SSHD_SETUP_REMOTE = "/content/_cterm_sshd.sh"

# {pubkey} is substituted with the session's Ed25519 public key line.
_SSHD_SETUP_SCRIPT = """\
#!/bin/bash
# cterm sshd setup — runs a private sshd on port 22 alongside any existing one
exec > /content/cterm_sshd_setup.log 2>&1
set -x

# Kill any stale cterm agent
pkill -f '_cterm_agent.py' 2>/dev/null || true

# Install openssh-server if missing (Colab images usually have it)
if ! command -v sshd &>/dev/null; then
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq openssh-server
fi

# Ensure host keys exist
ssh-keygen -A
mkdir -p /run/sshd

# Install session public key
mkdir -p /root/.ssh
chmod 700 /root/.ssh
echo "{pubkey}" > /root/.ssh/authorized_keys
chmod 600 /root/.ssh/authorized_keys

# Write a standalone config (separate from /etc/ssh/sshd_config which may set Port 2222)
cat > /tmp/cterm_sshd.conf << 'SSHEOF'
Port 22
ListenAddress 0.0.0.0
PermitRootLogin yes
PubkeyAuthentication yes
AuthorizedKeysFile /root/.ssh/authorized_keys
PasswordAuthentication no
UsePAM no
HostKey /etc/ssh/ssh_host_rsa_key
HostKey /etc/ssh/ssh_host_ecdsa_key
HostKey /etc/ssh/ssh_host_ed25519_key
SSHEOF

# Kill any previous cterm sshd (uses our config path as identifier), not the system one
pkill -f 'sshd.*cterm_sshd' 2>/dev/null || true
sleep 1

SSHD_BIN=$(command -v sshd 2>/dev/null || echo /usr/sbin/sshd)
"$SSHD_BIN" -f /tmp/cterm_sshd.conf
echo "[cterm] sshd exit=$?, checking port..."
ss -tlnp 2>/dev/null | grep ':22 ' || netstat -tlnp 2>/dev/null | grep ':22 ' || echo "[cterm] nothing on :22"
"""

# ------------------------------------------------------------------
# Keypair generation
# ------------------------------------------------------------------

def _generate_keypair() -> tuple[bytes, str]:
    """Return (private_key_PEM_bytes, public_key_OpenSSH_str).

    Uses the ``cryptography`` package (pulled in transitively by google-auth).
    """
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: PLC0415
    from cryptography.hazmat.primitives.serialization import (  # noqa: PLC0415
        Encoding,
        NoEncryption,
        PrivateFormat,
        PublicFormat,
    )

    key = Ed25519PrivateKey.generate()
    priv = key.private_bytes(Encoding.PEM, PrivateFormat.OpenSSH, NoEncryption())
    pub = key.public_key().public_bytes(Encoding.OpenSSH, PublicFormat.OpenSSH)
    return priv, pub.decode().strip()


# ------------------------------------------------------------------
# Find local ssh binary
# ------------------------------------------------------------------

def _find_ssh() -> str | None:
    """Return a path to the local ``ssh`` binary, or None if not found."""
    if shutil.which("ssh"):
        return "ssh"
    # Git for Windows fallback
    git_ssh = r"C:\Program Files\Git\usr\bin\ssh.exe"
    if os.path.isfile(git_ssh):
        return git_ssh
    return None


# ------------------------------------------------------------------
# Public entry point
# ------------------------------------------------------------------

def run_ssh(
    client: ColabClient,
    local_port: int = 2222,
    sshd_wait: int = 60,
    extra_ssh_args: list[str] | None = None,
) -> int:
    """Set up sshd on the VM and open an SSH session via the mux tunnel.

    extra_ssh_args are inserted into the ssh command before the host, so any
    SSH option is valid: -L/-R/-D for forwarding, -N to skip the shell, -v for
    debug, -o for config overrides, etc.  Example::

        run_ssh(client, extra_ssh_args=["-N", "-L", "8888:localhost:8888"])
    """
    print()
    print("  [EXPERIMENTAL] cterm ssh")
    print("  Interactive SSH shell via Colab's terminal WebSocket.")
    print("  Slower than direct SSH; fine for interactive use.")
    print()

    if not require_proxy(client):
        return 1

    ssh_bin = _find_ssh()
    if not ssh_bin:
        err(
            "No local 'ssh' client found on PATH. "
            "Install OpenSSH (Windows optional feature) or Git for Windows."
        )
        return 1

    # 1. Generate a throwaway keypair for this session.
    try:
        priv_bytes, pubkey = _generate_keypair()
    except Exception as exc:  # noqa: BLE001
        err(f"Could not generate SSH keypair: {exc}")
        return 1

    # 2. Upload the mux agent (skipped if unchanged on a warm runtime).
    if not upload_agent(client):
        return 1

    # 3. Upload and launch the sshd setup script on the VM.
    log("Configuring sshd on the VM...")
    script = _SSHD_SETUP_SCRIPT.format(pubkey=pubkey)
    if not upload_file(client, _SSHD_SETUP_REMOTE, script, "sshd setup script"):
        return 1
    if not run_script_in_terminal(client, _SSHD_SETUP_REMOTE, "sshd"):
        return 1

    # 4. Bind local listener before opening the tunnel so the port is ready
    #    the moment the ssh client tries to connect.
    server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        server_sock.bind(("127.0.0.1", local_port))
    except OSError as exc:
        err(f"Cannot bind to 127.0.0.1:{local_port}: {exc}")
        return 1
    server_sock.listen(4)
    server_sock.settimeout(0.5)

    stop_event = threading.Event()

    def _on_dead() -> None:
        stop_event.set()
        try:
            server_sock.close()
        except OSError:
            pass

    # 5. Open the data channel; agent polls port 22 until sshd is ready.
    tunnel = PtyTunnel(
        client,
        target_port=22,
        ready_timeout=sshd_wait + 15,
        agent_wait=sshd_wait,
        label="ssh",
        on_dead=_on_dead,
    )
    if not tunnel.open():
        server_sock.close()
        return 1

    if tunnel.dead.is_set():
        tunnel.close()
        server_sock.close()
        return 1

    # 6. Accept thread: forward local connections through the mux tunnel.
    def _accept_loop() -> None:
        while not stop_event.is_set():
            try:
                conn, _ = server_sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            conn.settimeout(None)
            threading.Thread(
                target=handle_local_conn, args=(conn, tunnel), daemon=True
            ).start()

    threading.Thread(target=_accept_loop, daemon=True).start()

    # 7. Write private key to a temp file and launch the ssh client.
    null_dev = "NUL" if os.name == "nt" else "/dev/null"
    tmp_key: str | None = None
    rc = 1
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", suffix=".pem", delete=False
        ) as f:
            f.write(priv_bytes)
            tmp_key = f.name

        if os.name != "nt":
            os.chmod(tmp_key, 0o600)

        extra = list(extra_ssh_args or [])
        log(
            f"Opening SSH -> root@127.0.0.1 via tunnel on port {local_port}"
            + (f" (extra args: {extra})" if extra else "")
            + "..."
        )
        time.sleep(0.3)  # brief pause to let accept thread settle

        # Mandatory auth flags come first so they cannot be overridden by accident.
        # User-supplied options (extra) are inserted immediately before the host so
        # that SSH sees them as option flags; any non-flag items (e.g. a remote
        # command) should be placed last by the caller.
        ssh_cmd = [
            ssh_bin,
            "-p", str(local_port),
            "-i", tmp_key,
            "-o", "StrictHostKeyChecking=no",
            "-o", f"UserKnownHostsFile={null_dev}",
            "-o", "PubkeyAuthentication=yes",
            "-o", "PreferredAuthentications=publickey",
            "-o", "IdentitiesOnly=yes",
            *extra,
            "root@127.0.0.1",
        ]
        try:
            result = subprocess.run(ssh_cmd)
            rc = result.returncode
        except FileNotFoundError:
            err(f"SSH binary not found: {ssh_bin}")
            rc = 1
    except KeyboardInterrupt:
        rc = 130
    finally:
        stop_event.set()
        try:
            server_sock.close()
        except OSError:
            pass
        tunnel.close()
        if tmp_key and os.path.exists(tmp_key):
            try:
                os.unlink(tmp_key)
            except OSError:
                pass

    if rc not in (0, 130):
        err(f"SSH exited with code {rc}.")
    return 0 if rc in (0, 130) else rc
