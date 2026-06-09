"""Bridge the local terminal to the Colab runtime over /colab/tty.

The extension's "Open Terminal" command (colab.openTerminal) connects to:

    wss://{runtimeProxyInfo.url host}/colab/tty

authenticated with X-Colab-Runtime-Proxy-Token (the runtime proxy token from
v1/runtime-proxy-token) plus X-Colab-Client-Agent: vscode.

Message protocol (JSON in both directions):
  Send input : {"data": "<keystrokes>"}
  Send resize: {"cols": N, "rows": M}
  Recv output: {"data": "<terminal output>"}
"""

from __future__ import annotations

import json
import os
import platform
import sys
import threading
import time
from urllib.parse import urlparse

import websocket  # websocket-client

from .client import ColabClient
from .constants import HDR_CLIENT_AGENT, HDR_PROXY_TOKEN
from .utils import err, get_terminal_size, log


class ColabTtyBridge:
    """Bridges the local terminal to the Colab runtime via /colab/tty."""

    def __init__(self, client: ColabClient) -> None:
        self.client = client
        self.ws: websocket.WebSocket | None = None
        self.running = False

    def connect(self) -> None:
        if not self.client.proxy_url:
            raise RuntimeError(
                "proxy_url not set - runtime proxy token was not acquired"
            )
        if not self.client.proxy_token:
            raise RuntimeError(
                "proxy_token not set - runtime proxy token was not acquired"
            )

        parsed = urlparse(self.client.proxy_url)
        ws_url = f"wss://{parsed.netloc}/colab/tty"

        headers = {
            HDR_PROXY_TOKEN: self.client.proxy_token,
            HDR_CLIENT_AGENT[0]: HDR_CLIENT_AGENT[1],
        }

        log(f"Connecting to Colab TTY at {ws_url} ...")
        self.ws = websocket.WebSocket()
        self.ws.connect(
            ws_url,
            header=[f"{k}: {v}" for k, v in headers.items()],
        )
        self.running = True
        log("Connected! You are now in a Colab shell. Press Ctrl+] to exit.\n")

        cols, rows = get_terminal_size()
        self._send_resize(cols, rows)

    # -- I/O helpers ------------------------------------------------------

    def _send_resize(self, cols: int, rows: int) -> None:
        if self.ws:
            try:
                self.ws.send(json.dumps({"cols": cols, "rows": rows}))
            except (websocket.WebSocketException, OSError):
                pass

    def _send_stdin(self, data: str) -> None:
        if self.ws:
            self.ws.send(json.dumps({"data": data}))

    def _recv_loop(self) -> None:
        while self.running and self.ws:
            try:
                raw = self.ws.recv()
                if not raw:
                    break
                msg = json.loads(raw)
                if "data" in msg:
                    sys.stdout.write(msg["data"])
                    sys.stdout.flush()
            except websocket.WebSocketConnectionClosedException:
                break
            except (json.JSONDecodeError, OSError):
                break
        self.running = False

    # -- main loop --------------------------------------------------------

    def run(self) -> None:
        if not self.ws:
            err("Not connected")
            return
        if platform.system() == "Windows":
            self._run_windows()
        else:
            self._run_unix()

    def _run_unix(self) -> None:
        import select as _select  # noqa: PLC0415
        import signal  # noqa: PLC0415
        import termios  # noqa: PLC0415  # type: ignore[import]
        import tty  # noqa: PLC0415

        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)

        def restore() -> None:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)

        def _on_winch(_sig, _frame) -> None:
            c, r = get_terminal_size()
            self._send_resize(c, r)

        signal.signal(signal.SIGWINCH, _on_winch)  # type: ignore[attr-defined]  # noqa: E501
        try:
            tty.setraw(fd)
            threading.Thread(target=self._recv_loop, daemon=True).start()
            while self.running:
                if _select.select([sys.stdin], [], [], 0.05)[0]:
                    data = os.read(fd, 4096)
                    if not data:
                        break
                    text = data.decode("utf-8", errors="replace")
                    if "\x1d" in text:  # Ctrl+]
                        break
                    self._send_stdin(text)
        except (KeyboardInterrupt, EOFError):
            pass
        finally:
            self.running = False
            restore()
            self._close_ws()
            print("\n[*] Disconnected from Colab terminal.")

    def _run_windows(self) -> None:
        import msvcrt  # noqa: PLC0415

        threading.Thread(target=self._recv_loop, daemon=True).start()
        try:
            while self.running:
                if msvcrt.kbhit():
                    ch = msvcrt.getwch()
                    if ch == "\x1d":  # Ctrl+]
                        break
                    self._send_stdin(ch)
                else:
                    time.sleep(0.01)
        except (KeyboardInterrupt, EOFError):
            pass
        finally:
            self.running = False
            self._close_ws()
            print("\n[*] Disconnected from Colab terminal.")

    def _close_ws(self) -> None:
        if self.ws:
            try:
                self.ws.close()
            except (websocket.WebSocketException, OSError):
                pass
