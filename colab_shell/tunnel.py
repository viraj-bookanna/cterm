"""Shared muxed-PTY tunnel infrastructure over Colab's Jupyter terminal WebSocket.

Architecture
------------

  Local machine                       Colab VM (Ubuntu)
  ────────────────                    ─────────────────────────────────────
  Caller (proxy / ssh)                _cterm_agent.py  (pty_mux)
      │                                   │  raw PTY, no echo
      │  TCP (:local_port)                │
      ▼                                   ▼
  Local listener ──── WSS ────────── agent → target service (port N)
                  /terminals/websocket/{name}

Protocol
--------
  outbound: ["stdin", "<base64-frame>\\n"]
  inbound:  ["stdout", "<base64-fragment>"]  (may arrive split across frames)

  Decoded frame:  connID (2 B big-endian) | type (1 B) | payload
    type 1 = OPEN   open a new connection to the target
    type 2 = DATA   forward payload bytes
    type 3 = CLOSE  tear down the connection

Readiness handshake
-------------------
  Agent emits __CTERM_READY__   once the target port is accepting connections.
  Agent emits __CTERM_NOPROXY__ if the target never becomes reachable and exits.
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

TYPE_OPEN = 1
TYPE_DATA = 2
TYPE_CLOSE = 3

_CHUNK = 4096  # max payload bytes per DATA frame


# ------------------------------------------------------------------
# Low-level Jupyter API helpers
# ------------------------------------------------------------------

def upload_file(
    client: ColabClient, remote_path: str, content: str, label: str
) -> bool:
    """Upload a UTF-8 text file to the runtime via the Jupyter contents API."""
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


def create_terminal(client: ColabClient) -> tuple[str, str]:
    """Create a Jupyter terminal and return *(term_name, ws_url)*.

    Raises ``RuntimeError`` (via resp.raise_for_status()) on failure.
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


def require_proxy(client: ColabClient) -> bool:
    """Return True when the client has a proxy URL and token, False otherwise."""
    if not client.proxy_url or not client.proxy_token:
        err("Runtime proxy URL/token not set.")
        return False
    return True


def _connect_terminal_ws(
    client: ColabClient, label: str
) -> tuple[websocket.WebSocket, str] | None:
    """Create a Jupyter terminal and open its WebSocket.

    Returns *(ws, term_name)* on success, or ``None`` on error.
    The caller is responsible for closing the WebSocket and deleting the terminal.
    """
    try:
        term_name, ws_url = create_terminal(client)
    except Exception as exc:  # noqa: BLE001
        err(f"Could not create terminal for {label}: {exc}")
        return None

    ws = websocket.WebSocket()
    try:
        ws.connect(
            ws_url,
            header=[f"X-Colab-Runtime-Proxy-Token: {client.proxy_token}"],
            sslopt={"context": make_ssl_context()},
        )
    except (websocket.WebSocketException, OSError) as exc:
        err(f"Could not connect terminal WebSocket for {label}: {exc}")
        return None

    return ws, term_name


def delete_terminal_bg(client: ColabClient, term_name: str) -> None:
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
# Agent upload
# ------------------------------------------------------------------

def upload_agent(client: ColabClient) -> bool:
    """Upload the VM agent (pty_mux.py), skipping if unchanged."""
    agent_src = Path(__file__).parent / "_agent" / "pty_mux.py"
    try:
        content = agent_src.read_text(encoding="utf-8")
    except OSError as exc:
        err(f"Cannot read agent source: {exc}")
        return False

    content_hash = hashlib.sha256(content.encode()).hexdigest()

    try:
        ver_model = client.contents_get(_AGENT_VER_REMOTE)
        remote_hash = (ver_model.get("content") or "").strip()
        if remote_hash == content_hash:
            log("Agent already up-to-date on VM; skipping upload.")
            return True
    except Exception:  # noqa: BLE001
        pass

    if not upload_file(client, _AGENT_REMOTE, content, "Agent"):
        return False

    upload_file(client, _AGENT_VER_REMOTE, content_hash + "\n", "Agent version marker")
    return True


# ------------------------------------------------------------------
# Short-lived terminal helpers
# ------------------------------------------------------------------

def run_script_in_terminal(
    client: ColabClient,
    remote_script_path: str,
    label: str,
    launch_cmd: str | None = None,
) -> bool:
    """Run a remote script via a short-lived Jupyter terminal.

    The command is launched under ``setsid`` so it survives the terminal
    close.  Used to start background daemons that do not require the IPython
    kernel.  Returns True if the command was dispatched without error; actual
    readiness for tunnel-based daemons is confirmed by ``PtyTunnel``'s
    ``__CTERM_READY__`` handshake.

    *launch_cmd* overrides the default ``setsid bash <script> &>/tmp/cterm_<label>.log &``.
    Pass an explicit command when the script is not a bash script (e.g. a Python
    daemon).  The string must end with ``&`` so the process is backgrounded and
    the terminal can be closed immediately.
    """
    result = _connect_terminal_ws(client, label)
    if result is None:
        return False
    ws, term_name = result

    if launch_cmd is None:
        cmd = f"setsid bash {remote_script_path} &>/tmp/cterm_{label}.log &\n"
    else:
        cmd = launch_cmd if launch_cmd.endswith("\n") else launch_cmd + "\n"

    try:
        time.sleep(0.5)
        ws.send(json.dumps(["stdin", cmd]))
        time.sleep(1.0)
    except (websocket.WebSocketException, OSError) as exc:
        err(f"Could not run {label}: {exc}")
        return False
    finally:
        try:
            ws.close()
        except (websocket.WebSocketException, OSError):
            pass

    delete_terminal_bg(client, term_name)
    log(f"{label} startup command sent.")
    return True


def run_terminal_capture(
    client: ColabClient,
    command: str,
    result_marker: str,
    timeout: float = 75.0,
) -> tuple[str, str]:
    """Run a shell command in a short-lived terminal and capture its output.

    Sends *command* to a fresh Jupyter terminal and reads ``stdout``/``stderr``
    frames until *result_marker* appears in the accumulated output or *timeout*
    seconds have elapsed.

    Returns ``(token, full_output)`` where *token* is the first whitespace-
    delimited word immediately following *result_marker* (empty string when the
    marker was not found before the timeout or on error).
    """
    result = _connect_terminal_ws(client, "capture")
    if result is None:
        return "", ""
    ws, term_name = result

    full_output: list[str] = []
    token = ""
    try:
        time.sleep(0.5)
        ws.send(json.dumps(["stdin", command + "\n"]))

        buf = ""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                ws.settimeout(1.0)
                raw = ws.recv()
            except websocket.WebSocketTimeoutException:
                continue
            except (websocket.WebSocketConnectionClosedException, OSError):
                break
            if not raw:
                break
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(msg, list) or msg[0] not in ("stdout", "stderr"):
                continue
            text = msg[1]
            if not isinstance(text, str):
                continue
            full_output.append(text)
            buf += text
            if result_marker in buf:
                idx = buf.index(result_marker) + len(result_marker)
                rest = buf[idx:].lstrip()
                parts = rest.split()
                token = parts[0] if parts else ""
                break
    except (websocket.WebSocketException, OSError) as exc:
        err(f"Terminal capture error: {exc}")
    finally:
        try:
            ws.close()
        except (websocket.WebSocketException, OSError):
            pass

    delete_terminal_bg(client, term_name)
    return token, "".join(full_output)


# ------------------------------------------------------------------
# PtyTunnel: long-lived data channel
# ------------------------------------------------------------------

class PtyTunnel:
    """Muxed TCP tunnel over a single Colab Jupyter terminal WebSocket.

    The remote agent (pty_mux.py) runs in raw/no-echo mode and multiplexes
    multiple TCP connections to ``target_host:target_port`` on the VM.

    Usage::

        tunnel = PtyTunnel(client, target_port=22, label="ssh")
        if not tunnel.open():
            return 1
        # use send_frame() / register() etc.
        tunnel.close()
    """

    READY_MARKER = "__CTERM_READY__"
    NOTARGET_MARKER = "__CTERM_NOPROXY__"  # wire sentinel kept for compatibility

    def __init__(
        self,
        client: ColabClient,
        target_port: int,
        target_host: str = "127.0.0.1",
        ready_timeout: int = 60,
        agent_wait: int = 40,
        label: str = "tunnel",
        on_dead: Callable[[], None] | None = None,
    ) -> None:
        self._client = client
        self._target_port = target_port
        self._target_host = target_host
        self._ready_timeout = ready_timeout
        self._agent_wait = agent_wait
        self._label = label
        self._on_dead = on_dead

        self._ws: websocket.WebSocket | None = None
        self._term_name: str = ""
        self._send_lock = threading.Lock()

        # connID → local socket that data is forwarded to
        self._conns: dict[int, socket.socket] = {}
        self._conns_lock = threading.Lock()
        self._next_id = 0

        self._ready_event = threading.Event()
        self.dead = threading.Event()
        self._inbuf = ""

    # -- lifecycle ----------------------------------------------------

    def open(self) -> bool:
        try:
            self._term_name, ws_url = create_terminal(self._client)
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
        threading.Thread(target=self._recv_loop, daemon=True).start()

        time.sleep(0.5)
        cmd = (
            f"stty raw -echo; "
            f"exec python3 {_AGENT_REMOTE} "
            f"{self._target_host} {self._target_port} {self._agent_wait}\n"
        )
        try:
            self._ws.send(json.dumps(["stdin", cmd]))
        except (websocket.WebSocketException, OSError) as exc:
            err(f"Could not send agent exec command: {exc}")
            return False

        log(f"Waiting for agent to reach {self._target_host}:{self._target_port} "
            f"(up to {self._ready_timeout} s)...")
        if not self._ready_event.wait(timeout=self._ready_timeout):
            err(f"Agent did not become ready within {self._ready_timeout} s.")
            return False

        if self.dead.is_set():
            return False

        log(f"Agent ready — {self._label} tunnel established.")
        return True

    def close(self) -> None:
        if self._ws:
            try:
                self._ws.close()
            except (websocket.WebSocketException, OSError):
                pass
        delete_terminal_bg(self._client, self._term_name)

    # -- frame I/O ----------------------------------------------------

    def send_frame(self, conn_id: int, ftype: int, payload: bytes = b"") -> None:
        """Encode one multiplexed frame and send it through the terminal stdin."""
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
        if not self.dead.is_set():
            err(
                f"[{self._label}] Data-channel WebSocket closed. "
                f"Re-run `cterm {self._label}` to reconnect."
            )
            self.dead.set()
            if self._on_dead:
                self._on_dead()

    def _recv_loop(self) -> None:
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
        """Parse complete newline-terminated lines from the inbound buffer."""
        while "\n" in self._inbuf:
            line, self._inbuf = self._inbuf.split("\n", 1)
            line = line.rstrip("\r")
            if not line:
                continue
            if not self._ready_event.is_set():
                if self.READY_MARKER in line:
                    self._ready_event.set()
                elif self.NOTARGET_MARKER in line:
                    err(
                        f"[{self._label}] Agent could not reach "
                        f"{self._target_host}:{self._target_port}. "
                        "The service may not be running on the VM."
                    )
                    self.dead.set()
                    self._ready_event.set()
                continue
            # After readiness: decode base64 data frames from the agent
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
        with self._conns_lock:
            for _ in range(0x10000):
                cid = self._next_id & 0xFFFF
                self._next_id += 1
                if cid not in self._conns:
                    return cid
        return self._next_id & 0xFFFF

    def register(self, conn_id: int, sock: socket.socket) -> None:
        with self._conns_lock:
            self._conns[conn_id] = sock

    def unregister(self, conn_id: int) -> None:
        with self._conns_lock:
            self._conns.pop(conn_id, None)


# ------------------------------------------------------------------
# Per-connection local bridge (shared by proxy and ssh)
# ------------------------------------------------------------------

def handle_local_conn(sock: socket.socket, tunnel: PtyTunnel) -> None:
    """Bridge one local TCP connection through the mux tunnel (one thread per conn)."""
    conn_id = tunnel.alloc_id()
    tunnel.register(conn_id, sock)
    tunnel.send_frame(conn_id, TYPE_OPEN)

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
