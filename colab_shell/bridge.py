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

import codecs
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
from .tls import make_ssl_context
from .utils import err, get_terminal_size, log


class ColabTtyBridge:
    """Bridges the local terminal to the Colab runtime via /colab/tty."""

    def __init__(
        self,
        client: ColabClient,
        startup_cmds: list[str] | None = None,
    ) -> None:
        self.client = client
        self.ws: websocket.WebSocket | None = None
        self.running = False
        # Set by the SIGWINCH handler on Unix; consumed by the main loop.
        self._winch = False
        # Commands sent to the remote shell after the session is ready.
        # Fired 1.5 s after the recv loop starts so the shell prompt is up.
        self._startup_cmds: list[str] = startup_cmds or []

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
            sslopt={"context": make_ssl_context()},
        )
        self.running = True
        log("Connected! You are now in a Colab shell.\n")

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
        if not self.ws:
            return
        try:
            self.ws.send(json.dumps({"data": data}))
        except (websocket.WebSocketException, OSError):
            self.running = False

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

    def _run_startup(self) -> None:
        """Send startup commands after a brief delay to let the shell settle."""
        time.sleep(1.5)
        for cmd in self._startup_cmds:
            if self.running:
                self._send_stdin(cmd)

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
        import termios  # noqa: PLC0415  # type: ignore[import]  # pylint: disable=import-error
        import tty  # noqa: PLC0415

        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)

        def restore() -> None:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)

        # Only set the flag in the signal handler; the main loop sends the
        # resize. This avoids re-entrant calls into websocket-client's
        # non-reentrant send lock while the main loop may already hold it.
        def _on_winch(_sig, _frame) -> None:
            self._winch = True

        signal.signal(signal.SIGWINCH, _on_winch)  # type: ignore[attr-defined]  # pylint: disable=no-member

        # Stateful decoder so multibyte UTF-8 chars split across read()
        # boundaries are assembled correctly instead of being corrupted.
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")

        try:
            tty.setraw(fd)
            threading.Thread(target=self._recv_loop, daemon=True).start()
            if self._startup_cmds:
                threading.Thread(target=self._run_startup, daemon=True).start()
            while self.running:
                if self._winch:
                    self._winch = False
                    self._send_resize(*get_terminal_size())
                if _select.select([sys.stdin], [], [], 0.05)[0]:
                    data = os.read(fd, 4096)
                    if not data:
                        break
                    text = decoder.decode(data)
                    self._send_stdin(text)
        except (KeyboardInterrupt, EOFError):
            pass
        finally:
            self.running = False
            restore()
            self._close_ws()
            print("\n[*] Disconnected from Colab terminal.")

    def _run_windows(self) -> None:
        import ctypes  # noqa: PLC0415
        import msvcrt  # noqa: PLC0415

        # Arrow / navigation keys arrive with the 0xe0 prefix.
        EXT_KEYS: dict[str, str] = {
            "H": "\x1b[A",  # Up
            "P": "\x1b[B",  # Down
            "M": "\x1b[C",  # Right
            "K": "\x1b[D",  # Left
            "G": "\x1b[H",  # Home
            "O": "\x1b[F",  # End
            "R": "\x1b[2~",  # Insert
            "S": "\x1b[3~",  # Delete
            "I": "\x1b[5~",  # PageUp
            "Q": "\x1b[6~",  # PageDown
        }

        # Function keys arrive with the 0x00 prefix.
        # Some layouts also emit nav keys via 0x00; include them here too.
        FN_KEYS: dict[str, str] = {
            # F1-F12
            ";": "\x1bOP",
            "<": "\x1bOQ",
            "=": "\x1bOR",
            ">": "\x1bOS",
            "?": "\x1b[15~",
            "@": "\x1b[17~",
            "A": "\x1b[18~",
            "B": "\x1b[19~",
            "C": "\x1b[20~",
            "D": "\x1b[21~",
            "\x85": "\x1b[23~",
            "\x86": "\x1b[24~",
            # Nav keys (some keyboard layouts / numpad)
            "H": "\x1b[A",
            "P": "\x1b[B",
            "M": "\x1b[C",
            "K": "\x1b[D",
            "G": "\x1b[H",
            "O": "\x1b[F",
            "R": "\x1b[2~",
            "S": "\x1b[3~",
            "I": "\x1b[5~",
            "Q": "\x1b[6~",
        }

        # Disable ENABLE_PROCESSED_INPUT so Ctrl+C is delivered as \x03
        # through msvcrt.getwch() instead of raising KeyboardInterrupt.
        ENABLE_PROCESSED_INPUT = 0x0001
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        h_in = kernel32.GetStdHandle(-10)
        old_mode = ctypes.c_uint()
        have_mode = bool(kernel32.GetConsoleMode(h_in, ctypes.byref(old_mode)))
        if have_mode:
            kernel32.SetConsoleMode(h_in, old_mode.value & ~ENABLE_PROCESSED_INPUT)

        # Poll size every 500 ms and forward a resize when it changes.
        last_size = get_terminal_size()
        last_check = time.time()

        threading.Thread(target=self._recv_loop, daemon=True).start()
        if self._startup_cmds:
            threading.Thread(target=self._run_startup, daemon=True).start()
        try:
            while self.running:
                now = time.time()
                if now - last_check > 0.5:
                    last_check = now
                    size = get_terminal_size()
                    if size != last_size:
                        last_size = size
                        self._send_resize(*size)

                if msvcrt.kbhit():
                    ch = msvcrt.getwch()
                    if ch == "\xe0":  # extended key prefix (arrows, nav)
                        seq = EXT_KEYS.get(msvcrt.getwch())
                        if seq:
                            self._send_stdin(seq)
                        continue
                    if ch == "\x00":  # function-key prefix
                        seq = FN_KEYS.get(msvcrt.getwch())
                        if seq:
                            self._send_stdin(seq)
                        continue
                    self._send_stdin(ch)
                else:
                    time.sleep(0.01)
        except EOFError:
            pass
        finally:
            if have_mode:
                kernel32.SetConsoleMode(h_in, old_mode.value)
            self.running = False
            self._close_ws()
            print("\n[*] Disconnected from Colab terminal.")

    def _close_ws(self) -> None:
        if self.ws:
            try:
                self.ws.close()
            except (websocket.WebSocketException, OSError):
                pass
