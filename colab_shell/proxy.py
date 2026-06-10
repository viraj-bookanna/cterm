"""Experimental: route local traffic through the Colab runtime.

Architecture
------------

  Local machine                        Colab VM (Ubuntu)
  ─────────────────                    ──────────────────────────────────────
  Browser / curl                       pproxy  (HTTP+SOCKS5 on 127.0.0.1:8764)
      │                                    ▲
      │ TCP (:LOCAL_PORT)                   │ TCP (127.0.0.1 only)
      ▼                                     │
  cterm proxy listener                 _cterm_agent.py  (pty_mux)
      │  (this file)                        │  runs inside Jupyter terminal
      │                                     │  raw PTY, no echo
      └── _PtyTunnel ─── WSS ────────────── ┘
              /terminals/websocket/{name}
              (Colab's Jupyter gateway — Google-hosted)

All traffic stays within Google infrastructure: the only outbound TCP is from
the VM's pproxy to the final target site.

Protocol
--------
The Jupyter terminal WebSocket carries Terminado envelopes:
  outbound: ["stdin", "<ascii-base64>\\n"]
  inbound:  ["stdout", "<ascii-base64>..."]  (may arrive in fragments)

Each decoded frame:  connID (2 bytes big-endian) | type (1 byte) | payload
  type 1 = OPEN  – open a new connection to pproxy
  type 2 = DATA  – forward payload bytes
  type 3 = CLOSE – close the connection

Readiness handshake
-------------------
The agent polls pproxy internally and only emits __CTERM_READY__ once pproxy
is accepting connections.  If pproxy never starts it emits __CTERM_NOPROXY__
and exits; the local side fails fast with a clear error.

Steps
-----
1. Upload pty_mux.py agent to /content/_cterm_agent.py via contents API.
   A SHA-256 marker file (/content/_cterm_agent.ver) is written alongside it;
   on subsequent runs the upload is skipped if the content is unchanged.
2. Start pproxy (HTTP+SOCKS5 on 127.0.0.1:8764) via a short-lived terminal.
   The startup terminal is deleted after the command is sent.
3. Open a dedicated data-channel Jupyter terminal; exec the agent in raw mode.
4. Agent polls pproxy; on success emits __CTERM_READY__ (or __CTERM_NOPROXY__
   on failure).
5. Bind a local TCP listener on --port (default 1080).
   Each accepted connection gets a connID; all are multiplexed over the single
   terminal WebSocket.
"""

from __future__ import annotations

import base64
import hashlib
import json
import select
import socket
import threading
import time
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

import websocket  # websocket-client

from .client import ColabClient
from .tls import make_ssl_context
from .utils import err, log

# ------------------------------------------------------------------
# Constants
# ------------------------------------------------------------------

_AGENT_REMOTE = "/content/_cterm_agent.py"
_AGENT_VER_REMOTE = "/content/_cterm_agent.ver"
_PPROXY_SCRIPT_REMOTE = "/content/_cterm_pproxy.sh"

TYPE_OPEN = 1
TYPE_DATA = 2
TYPE_CLOSE = 3

_CHUNK = 4096  # max payload bytes per DATA frame

# Shell script that starts pproxy in the background (setsid so it
# survives after the short-lived terminal is closed).
_PPROXY_SCRIPT = """\
#!/bin/bash
# Kill stale instances
pkill -f 'pproxy.*{proxy_port}' 2>/dev/null || true
pkill -f '_cterm_agent.py' 2>/dev/null || true
sleep 0.3

# pproxy is incompatible with uvloop on Python 3.12
pip install -q pproxy 2>/dev/null
pip uninstall -y uvloop 2>/dev/null || true

pproxy -l "http+socks5://127.0.0.1:{proxy_port}" >/tmp/pproxy.log 2>&1 &
echo "[cterm] pproxy pid=$!"
"""


# ------------------------------------------------------------------
# Shared helpers
# ------------------------------------------------------------------

def _upload_file(
    client: ColabClient, remote_path: str, content: str, label: str
) -> bool:
    """Upload a text file to the runtime via the Jupyter contents API."""
    try:
        client.contents_put(
            remote_path,
            {"type": "file", "format": "text", "content": content},
        )
        log(f"{label} uploaded to {remote_path}")
        return True
    except (OSError, RuntimeError) as exc:
        err(f"Failed to upload {label}: {exc}")
        return False


def _create_terminal(client: ColabClient) -> tuple[str, str]:
    """Create a Jupyter terminal and return *(term_name, ws_url)*.

    Raises RuntimeError on failure.
    """
    proxy_url = client.proxy_url.rstrip("/")
    proxy_token = client.proxy_token

    resp = client.session.post(
        f"{proxy_url}/api/terminals",
        headers={
            "X-Colab-Runtime-Proxy-Token": proxy_token,
            "Content-Type": "application/json",
        },
        json={},
        timeout=15,
    )
    resp.raise_for_status()
    term_name = resp.json().get("name", "1")

    parsed = urlparse(
        proxy_url if proxy_url.startswith("https://") else "https://" + proxy_url
    )
    ws_url = f"wss://{parsed.netloc}/terminals/websocket/{term_name}"
    return term_name, ws_url


def _delete_terminal_bg(client: ColabClient, term_name: str) -> None:
    """Delete a Jupyter terminal in a daemon thread (best-effort)."""
    def _worker() -> None:
        try:
            proxy_url = client.proxy_url.rstrip("/")
            client.session.delete(
                f"{proxy_url}/api/terminals/{term_name}",
                headers={"X-Colab-Runtime-Proxy-Token": client.proxy_token},
                timeout=3,
            )
        except Exception:  # noqa: BLE001
            pass

    threading.Thread(target=_worker, daemon=True).start()


# ------------------------------------------------------------------
# Upload helpers
# ------------------------------------------------------------------

def _upload_agent(client: ColabClient) -> bool:
    """Upload the VM agent, skipping if the content is unchanged."""
    agent_src = Path(__file__).parent / "_agent" / "pty_mux.py"
    try:
        content = agent_src.read_text(encoding="utf-8")
    except OSError as exc:
        err(f"Cannot read agent source: {exc}")
        return False

    content_hash = hashlib.sha256(content.encode()).hexdigest()

    # Check whether the version already on the VM matches.
    try:
        ver_model = client.contents_get(_AGENT_VER_REMOTE)
        remote_hash = (ver_model.get("content") or "").strip()
        if remote_hash == content_hash:
            log("Agent already up-to-date on VM; skipping upload.")
            return True
    except Exception:  # noqa: BLE001
        pass  # Missing or unreadable — fall through to upload.

    if not _upload_file(client, _AGENT_REMOTE, content, "Agent"):
        return False

    # Write the version marker so next run can skip the upload.
    _upload_file(client, _AGENT_VER_REMOTE, content_hash + "\n", "Agent version marker")
    return True


def _upload_pproxy_script(client: ColabClient, proxy_port: int) -> bool:
    script = _PPROXY_SCRIPT.format(proxy_port=proxy_port)
    return _upload_file(client, _PPROXY_SCRIPT_REMOTE, script, "pproxy script")


# ------------------------------------------------------------------
# pproxy startup (short-lived terminal)
# ------------------------------------------------------------------

def _start_pproxy(client: ColabClient) -> bool:
    """Run the pproxy startup script in a short-lived Jupyter terminal.

    The terminal is deleted immediately after the command is dispatched;
    the setsid process is in its own session so it survives the closure.
    """
    proxy_token = client.proxy_token
    try:
        term_name, ws_url = _create_terminal(client)
    except Exception as exc:  # noqa: BLE001
        err(f"Could not create terminal for pproxy: {exc}")
        return False

    cmd = f"setsid bash {_PPROXY_SCRIPT_REMOTE} &>/tmp/cterm_pproxy.log &\n"
    ws = websocket.WebSocket()
    try:
        ws.connect(
            ws_url,
            header=[f"X-Colab-Runtime-Proxy-Token: {proxy_token}"],
            sslopt={"context": make_ssl_context()},
        )
        time.sleep(0.5)
        ws.send(json.dumps(["stdin", cmd]))
        time.sleep(1.0)
    except (websocket.WebSocketException, OSError) as exc:
        err(f"Could not start pproxy: {exc}")
        return False
    finally:
        try:
            ws.close()
        except (websocket.WebSocketException, OSError):
            pass

    # Delete the startup terminal; the setsid process lives on independently.
    _delete_terminal_bg(client, term_name)

    log("pproxy startup command sent.")
    return True


# ------------------------------------------------------------------
# _PtyTunnel: the long-lived data-channel
# ------------------------------------------------------------------

class _PtyTunnel:
    """Holds the Jupyter terminal WebSocket that carries multiplexed frames.

    The agent runs in raw/no-echo mode inside that terminal so the PTY
    layer passes base64-encoded frames through unchanged.

    The agent polls pproxy internally and only emits __CTERM_READY__ once
    pproxy is accepting connections, so no fixed sleep is needed here.
    If it emits __CTERM_NOPROXY__ instead, open() returns False immediately.

    When the data channel drops unexpectedly, the `dead` Event is set and
    the `on_dead` callback (if provided) is invoked.  The caller uses this
    to close the local listener and exit cleanly.
    """

    READY_MARKER = "__CTERM_READY__"
    NOPROXY_MARKER = "__CTERM_NOPROXY__"

    def __init__(
        self,
        client: ColabClient,
        proxy_port: int,
        ready_timeout: int = 60,
        on_dead: Callable[[], None] | None = None,
    ) -> None:
        self._client = client
        self._proxy_port = proxy_port
        self._ready_timeout = ready_timeout
        self._on_dead = on_dead

        self._ws: websocket.WebSocket | None = None
        self._term_name: str = ""
        # _send_lock guards all WebSocket sends after _recv_loop starts.
        self._send_lock = threading.Lock()

        # connID -> local socket
        self._conns: dict[int, socket.socket] = {}
        self._conns_lock = threading.Lock()
        self._next_id = 0

        # Set when agent sends __CTERM_READY__ (or __CTERM_NOPROXY__)
        self._ready_event = threading.Event()
        # Set when the data channel WebSocket closes unexpectedly
        self.dead = threading.Event()
        # Accumulate partial terminal output between newlines
        self._inbuf = ""

    # -- lifecycle ----------------------------------------------------

    def open(self) -> bool:
        try:
            self._term_name, ws_url = _create_terminal(self._client)
        except Exception as exc:  # noqa: BLE001
            err(f"Could not create data-channel terminal: {exc}")
            return False

        proxy_token = self._client.proxy_token
        self._ws = websocket.WebSocket()
        try:
            self._ws.connect(
                ws_url,
                header=[f"X-Colab-Runtime-Proxy-Token: {proxy_token}"],
                sslopt={"context": make_ssl_context()},
            )
        except (websocket.WebSocketException, OSError) as exc:
            err(f"Could not connect data-channel WebSocket: {exc}")
            return False

        log(f"Data-channel terminal opened (name={self._term_name})")

        # Start background reader before we launch the agent.
        threading.Thread(target=self._recv_loop, daemon=True).start()

        # Give bash a moment to produce its prompt, then exec the agent.
        # _send_raw is called here, before _recv_loop can interleave sends,
        # so no lock is needed for this single setup write.
        time.sleep(0.5)
        cmd = (
            f"stty raw -echo; "
            f"exec python3 {_AGENT_REMOTE} 127.0.0.1 {self._proxy_port}\n"
        )
        try:
            self._ws.send(json.dumps(["stdin", cmd]))
        except (websocket.WebSocketException, OSError) as exc:
            err(f"Could not send agent exec command: {exc}")
            return False

        log(
            f"Waiting for agent to be ready "
            f"(up to {self._ready_timeout} s while pproxy installs)..."
        )
        if not self._ready_event.wait(timeout=self._ready_timeout):
            err(
                f"Agent did not become ready within {self._ready_timeout} s. "
                "Check /tmp/pproxy.log on the VM for details."
            )
            return False

        log("Agent ready — data channel established.")
        return True

    def close(self) -> None:
        if self._ws:
            try:
                self._ws.close()
            except (websocket.WebSocketException, OSError):
                pass
        # Best-effort terminal cleanup in a daemon thread — Ctrl+C stays snappy.
        _delete_terminal_bg(self._client, self._term_name)

    # -- frame I/O ----------------------------------------------------

    def send_frame(self, conn_id: int, ftype: int, payload: bytes = b"") -> None:
        """Encode and send one multiplexed frame through the terminal stdin."""
        raw = conn_id.to_bytes(2, "big") + bytes([ftype]) + payload
        line = base64.b64encode(raw).decode() + "\n"
        with self._send_lock:
            if self._ws:
                try:
                    self._ws.send(json.dumps(["stdin", line]))
                except (websocket.WebSocketException, OSError):
                    pass

    # -- inbound demux ------------------------------------------------

    def _signal_dead(self) -> None:
        """Mark the tunnel as dead and notify the caller (once)."""
        if not self.dead.is_set():
            err(
                "[proxy] Data-channel WebSocket closed. "
                "Re-run `cterm proxy` to restore the tunnel."
            )
            self.dead.set()
            if self._on_dead:
                self._on_dead()

    def _recv_loop(self) -> None:
        """Background thread: read Terminado stdout envelopes and demux frames."""
        while True:
            try:
                raw_msg = self._ws.recv()
            except (websocket.WebSocketConnectionClosedException, OSError):
                self._signal_dead()
                break
            except websocket.WebSocketTimeoutException:
                continue
            except Exception:  # noqa: BLE001
                continue
            if not raw_msg:
                self._signal_dead()
                break
            try:
                msg = json.loads(raw_msg)
            except json.JSONDecodeError:
                continue
            if not isinstance(msg, list) or msg[0] not in ("stdout", "stderr"):
                continue
            text = msg[1]
            if not isinstance(text, str):
                continue
            self._inbuf += text
            try:
                self._process_buf()
            except Exception:  # noqa: BLE001
                pass

    def _process_buf(self) -> None:
        """Parse complete lines from the inbound buffer."""
        while "\n" in self._inbuf:
            line, self._inbuf = self._inbuf.split("\n", 1)
            line = line.rstrip("\r")
            if not line:
                continue
            if not self._ready_event.is_set():
                if self.READY_MARKER in line:
                    self._ready_event.set()
                elif self.NOPROXY_MARKER in line:
                    err(
                        "[proxy] Agent could not reach pproxy. "
                        "pproxy may have failed to install or start. "
                        "Check /tmp/pproxy.log on the VM."
                    )
                    self.dead.set()
                    self._ready_event.set()
                continue
            # After readiness: decode base64 frame
            try:
                data = base64.b64decode(line)
            except Exception:  # noqa: BLE001
                continue
            if len(data) < 3:
                continue
            conn_id = int.from_bytes(data[:2], "big")
            ftype = data[2]
            payload = data[3:]
            self._dispatch(conn_id, ftype, payload)

    def _dispatch(self, conn_id: int, ftype: int, payload: bytes) -> None:
        if ftype == TYPE_DATA and payload:
            with self._conns_lock:
                sock = self._conns.get(conn_id)
            if sock:
                try:
                    sock.sendall(payload)
                except OSError:
                    pass
        elif ftype == TYPE_CLOSE:
            with self._conns_lock:
                sock = self._conns.pop(conn_id, None)
            if sock:
                try:
                    sock.close()
                except OSError:
                    pass

    # -- connection management ----------------------------------------

    def alloc_id(self) -> int:
        # _conns_lock alone is sufficient — _id_lock was redundant.
        with self._conns_lock:
            for _ in range(0x10000):
                cid = self._next_id & 0xFFFF
                self._next_id += 1
                if cid not in self._conns:
                    return cid
        # All 65536 IDs in use — extremely unlikely; fall back.
        return self._next_id & 0xFFFF

    def register(self, conn_id: int, sock: socket.socket) -> None:
        with self._conns_lock:
            self._conns[conn_id] = sock

    def unregister(self, conn_id: int) -> None:
        with self._conns_lock:
            self._conns.pop(conn_id, None)


# ------------------------------------------------------------------
# Per-connection local bridge
# ------------------------------------------------------------------

def _handle_local_conn(
    sock: socket.socket,
    tunnel: _PtyTunnel,
) -> None:
    """Bridge a single local TCP connection through the tunnel."""
    conn_id = tunnel.alloc_id()
    tunnel.register(conn_id, sock)

    # Tell the VM agent to open a new pproxy connection.
    tunnel.send_frame(conn_id, TYPE_OPEN)

    # Pump local → tunnel as DATA frames.
    # select() avoids setting a timeout on the socket object, which would
    # otherwise interfere with _dispatch's sendall() on the _recv_loop thread
    # (socket timeout is shared across all threads using the same socket).
    try:
        while True:
            rlist, _, _ = select.select([sock], [], [], 0.5)
            if not rlist:
                continue
            data = sock.recv(_CHUNK)
            if not data:
                break
            tunnel.send_frame(conn_id, TYPE_DATA, data)
    except OSError:
        pass
    finally:
        tunnel.send_frame(conn_id, TYPE_CLOSE)
        tunnel.unregister(conn_id)
        try:
            sock.close()
        except OSError:
            pass


# ------------------------------------------------------------------
# Public entry point
# ------------------------------------------------------------------

def run_proxy(
    client: ColabClient,
    local_port: int,
    vm_proxy_port: int,
) -> int:
    """Set up and run the Colab-only proxy tunnel."""
    print()
    print("  [EXPERIMENTAL] cterm proxy")
    print("  Traffic tunnels via Colab's terminal WebSocket (Google-hosted only).")
    print("  Suitable for light use; large transfers may be slow.")
    print()

    if not client.proxy_url or not client.proxy_token:
        err("Runtime proxy URL/token not set.")
        return 1

    # 1. Upload the agent (skipped if unchanged on a warm runtime).
    if not _upload_agent(client):
        return 1

    # 2. Upload the pproxy startup script.
    if not _upload_pproxy_script(client, vm_proxy_port):
        return 1

    # 3. Start pproxy on the VM (runs in background via setsid).
    log("Starting pproxy on the VM...")
    if not _start_pproxy(client):
        return 1

    # 4. Open the data channel.  The agent polls pproxy and only emits
    #    __CTERM_READY__ once pproxy is accepting connections.
    server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

    def _on_dead() -> None:
        """Called from _recv_loop when the data channel dies."""
        try:
            server_sock.close()
        except OSError:
            pass

    tunnel = _PtyTunnel(client, vm_proxy_port, on_dead=_on_dead)
    if not tunnel.open():
        return 1

    # open() sets ready_event even for __CTERM_NOPROXY__ so wait() returns;
    # check tunnel.dead to distinguish failure from success.
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
    log(f"Listening on 127.0.0.1:{local_port}  (HTTP+SOCKS5 via Colab terminal)")
    log("Press Ctrl+C to stop.\n")

    try:
        while True:
            try:
                conn, _addr = server_sock.accept()
            except OSError:
                break
            threading.Thread(
                target=_handle_local_conn,
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
