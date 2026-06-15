"""Experimental: route local traffic through the Colab runtime via pproxy.

Architecture
------------

  Local machine                        Colab VM (Ubuntu)
  ─────────────────                    ──────────────────────────────────────
  Browser / curl                       pproxy  (HTTP+SOCKS5 on 127.0.0.1:8764)
      │                                    ▲
      │ TCP (:LOCAL_PORT)                  │ TCP (127.0.0.1 only)
      ▼                                    │
  cterm proxy listener                 _cterm_agent.py  (tunnel.PtyTunnel)
      │  (this file)                       │  runs inside Jupyter terminal
      │                                    │  raw PTY, no echo
      └── PtyTunnel ──── WSS ─────────────┘
              /terminals/websocket/{name}
              (Colab's Jupyter gateway — Google-hosted)

The shared tunnel infrastructure (PtyTunnel, agent upload, terminal helpers)
lives in ``colab_shell/tunnel.py``.  This module adds the pproxy-specific
startup script and the public ``run_proxy`` entry point.

All traffic stays within Google infrastructure: the only outbound TCP is from
the VM's pproxy to the final target site.
"""

from __future__ import annotations

import socket
import threading

from .client import ColabClient
from .tunnel import (
    PtyTunnel,
    handle_local_conn,
    run_script_in_terminal,
    upload_agent,
    upload_file,
)
from .utils import err, log

# ------------------------------------------------------------------
# pproxy startup scripts
# ------------------------------------------------------------------

_PPROXY_SCRIPT_REMOTE = "/content/_cterm_pproxy.sh"

_PPROXY_SCRIPT = """\
#!/bin/bash
# Kill stale instances
pkill -f 'pproxy.*{proxy_port}' 2>/dev/null || true
pkill -f '_cterm_agent.py' 2>/dev/null || true
sleep 0.3

# pproxy is incompatible with uvloop on Python 3.12+
pip install -q pproxy 2>/dev/null
pip uninstall -y uvloop 2>/dev/null || true

pproxy -l "http+socks5://127.0.0.1:{proxy_port}" {remote_arg} >/tmp/pproxy.log 2>&1 &
echo "[cterm] pproxy pid=$!"
"""

_TOR_SETUP_SCRIPT = """\
#!/bin/bash
# Kill stale instances (pproxy, agent, tor)
pkill -f 'pproxy.*{proxy_port}' 2>/dev/null || true
pkill -f '_cterm_agent.py' 2>/dev/null || true
pkill -x tor 2>/dev/null || true
sleep 0.3

# Install Tor if not present
if ! command -v tor &>/dev/null; then
    echo "[cterm] Installing tor..."
    apt-get update -qq && apt-get install -y -qq tor
fi

# Clear old log and start Tor
rm -f /tmp/tor.log
tor --SocksPort 9050 --Log "notice file /tmp/tor.log" --DataDirectory /tmp/tor_data &
echo "[cterm] tor pid=$!"

# Wait for Tor to bootstrap (up to 120 s)
for i in $(seq 1 120); do
    if grep -q "Bootstrapped 100%" /tmp/tor.log 2>/dev/null; then
        echo "[cterm] Tor bootstrapped."
        break
    fi
    sleep 1
done
if ! grep -q "Bootstrapped 100%" /tmp/tor.log 2>/dev/null; then
    echo "[cterm] WARNING: Tor may not have fully bootstrapped. Continuing anyway."
fi

# pproxy is incompatible with uvloop on Python 3.12+
pip install -q pproxy 2>/dev/null
pip uninstall -y uvloop 2>/dev/null || true

pproxy -l "http+socks5://127.0.0.1:{proxy_port}" -r "socks5://127.0.0.1:9050" >/tmp/pproxy.log 2>&1 &
echo "[cterm] pproxy pid=$!"
"""


def _upload_pproxy_script(
    client: ColabClient, proxy_port: int, use_tor: bool = False
) -> bool:
    if use_tor:
        script = _TOR_SETUP_SCRIPT.format(proxy_port=proxy_port)
        label = "pproxy+tor script"
    else:
        script = _PPROXY_SCRIPT.format(proxy_port=proxy_port, remote_arg="")
        label = "pproxy script"
    return upload_file(client, _PPROXY_SCRIPT_REMOTE, script, label)


# ------------------------------------------------------------------
# Public entry point
# ------------------------------------------------------------------

def run_proxy(
    client: ColabClient,
    local_port: int,
    vm_proxy_port: int,
    use_tor: bool = False,
) -> int:
    """Set up and run the Colab-only proxy tunnel."""
    print()
    if use_tor:
        print("  [EXPERIMENTAL] cterm proxy --tor")
        print("  Traffic exits via the Tor network (installed on the Colab VM).")
        print("  First run installs Tor via apt and waits for bootstrap (~2 min).")
    else:
        print("  [EXPERIMENTAL] cterm proxy")
        print("  Traffic tunnels via Colab's terminal WebSocket (Google-hosted only).")
        print("  Suitable for light use; large transfers may be slow.")
    print()

    if not client.proxy_url or not client.proxy_token:
        err("Runtime proxy URL/token not set.")
        return 1

    # Tor bootstrap + pproxy launch can take up to ~150 s on a cold runtime.
    agent_wait = 240 if use_tor else 40
    ready_timeout = 300 if use_tor else 60

    # 1. Upload the agent (skipped if unchanged on a warm runtime).
    if not upload_agent(client):
        return 1

    # 2. Upload the startup script (pproxy or pproxy+tor).
    if not _upload_pproxy_script(client, vm_proxy_port, use_tor=use_tor):
        return 1

    # 3. Start pproxy (and optionally Tor) on the VM via a short-lived terminal.
    engine = "Tor + pproxy" if use_tor else "pproxy"
    log(f"Starting {engine} on the VM...")
    if not run_script_in_terminal(client, _PPROXY_SCRIPT_REMOTE, "pproxy"):
        return 1

    # 4. Open the data channel.
    server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

    def _on_dead() -> None:
        try:
            server_sock.close()
        except OSError:
            pass

    tunnel = PtyTunnel(
        client,
        target_port=vm_proxy_port,
        ready_timeout=ready_timeout,
        agent_wait=agent_wait,
        label="proxy",
        on_dead=_on_dead,
    )
    if not tunnel.open():
        return 1

    if tunnel.dead.is_set():
        tunnel.close()
        return 1

    # 5. Local TCP listener.
    try:
        server_sock.bind(("127.0.0.1", local_port))
    except OSError as exc:
        err(f"Cannot bind to 127.0.0.1:{local_port}: {exc}")
        tunnel.close()
        return 1
    server_sock.listen(16)
    server_sock.settimeout(0.5)
    via = "Tor via Colab terminal" if use_tor else "Colab terminal"
    log(f"Listening on 127.0.0.1:{local_port}  (HTTP+SOCKS5 via {via})")
    log("Press Ctrl+C to stop.\n")

    try:
        while True:
            try:
                conn, _addr = server_sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            conn.settimeout(None)
            threading.Thread(
                target=handle_local_conn,
                args=(conn, tunnel),
                daemon=True,
            ).start()
    except KeyboardInterrupt:
        pass
    finally:
        try:
            server_sock.close()
        except OSError:
            pass
        tunnel.close()

    if tunnel.dead.is_set():
        return 1

    log("Proxy stopped.")
    return 0
