"""Multiplexed PTY tunnel agent — runs on the Colab VM.

Uploaded by ``cterm proxy`` to ``/content/_cterm_agent.py`` and started inside
a Jupyter terminal in raw/no-echo mode:

    stty raw -echo; exec python3 /content/_cterm_agent.py 127.0.0.1 8764

Protocol (stdin → agent → stdout, all through the Jupyter terminal WebSocket):
  - Each wire line is: base64(frame) + '\\n'
  - Decoded frame bytes: connID (2 bytes big-endian) | type (1 byte) | payload
      type 1 = OPEN   (payload ignored)
      type 2 = DATA   (payload = raw proxy bytes)
      type 3 = CLOSE  (payload ignored)
  - Agent polls pproxy until reachable, then prints '__CTERM_READY__\\n'.
  - If pproxy never starts it prints '__CTERM_NOPROXY__\\n' and exits.

Pure stdlib — no external dependencies.
"""

from __future__ import annotations

import base64
import os
import queue
import socket
import sys
import threading
import time

PROXY_HOST = "127.0.0.1"
PROXY_PORT = 8764

TYPE_OPEN = 1
TYPE_DATA = 2
TYPE_CLOSE = 3

# Sentinel pushed into a write queue to tell the writer thread to exit.
_QUEUE_STOP = object()

# Single connection map: conn_id -> (pproxy_socket, write_queue).
# One lock covers both the socket and the queue so every _handle_frame
# branch needs only a single acquisition.
_conns: dict[int, tuple[socket.socket, queue.Queue]] = {}
_conns_lock = threading.Lock()

_stdout_lock = threading.Lock()


def _write_frame(conn_id: int, ftype: int, payload: bytes = b"") -> None:
    raw = conn_id.to_bytes(2, "big") + bytes([ftype]) + payload
    data = base64.b64encode(raw) + b"\n"
    with _stdout_lock:
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()


def _pump_to_local(conn_id: int, sock: socket.socket) -> None:
    """Read from pproxy and send DATA frames back to local."""
    try:
        while True:
            data = sock.recv(4096)
            if not data:
                break
            _write_frame(conn_id, TYPE_DATA, data)
    except OSError:
        pass
    finally:
        _write_frame(conn_id, TYPE_CLOSE)
        with _conns_lock:
            entry = _conns.pop(conn_id, None)
        try:
            sock.close()
        except OSError:
            pass
        # Signal the writer thread to exit.
        if entry is not None:
            entry[1].put(_QUEUE_STOP)


def _pump_to_pproxy(conn_id: int, sock: socket.socket, wq: queue.Queue) -> None:
    """Drain the per-connection write queue into pproxy.

    Runs in its own thread so a stalled pproxy write never blocks the main
    demux loop (which would freeze all other connections).
    """
    try:
        while True:
            item = wq.get()
            if item is _QUEUE_STOP:
                break
            try:
                sock.sendall(item)
            except OSError:
                break
    finally:
        with _conns_lock:
            _conns.pop(conn_id, None)


def _handle_frame(conn_id: int, ftype: int, payload: bytes) -> None:
    if ftype == TYPE_OPEN:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.connect((PROXY_HOST, PROXY_PORT))
        except OSError:
            _write_frame(conn_id, TYPE_CLOSE)
            return
        wq: queue.Queue = queue.Queue()
        with _conns_lock:
            _conns[conn_id] = (sock, wq)
        threading.Thread(
            target=_pump_to_local, args=(conn_id, sock), daemon=True
        ).start()
        threading.Thread(
            target=_pump_to_pproxy, args=(conn_id, sock, wq), daemon=True
        ).start()

    elif ftype == TYPE_DATA:
        with _conns_lock:
            entry = _conns.get(conn_id)
        if entry is not None:
            entry[1].put(payload)

    elif ftype == TYPE_CLOSE:
        with _conns_lock:
            entry = _conns.pop(conn_id, None)
        if entry is not None:
            sock, wq = entry
            wq.put(_QUEUE_STOP)
            try:
                sock.close()
            except OSError:
                pass


def _enter_raw_mode() -> None:
    try:
        import tty
        import termios
        tty.setraw(sys.stdin.fileno())
        # Disable OPOST so \n in output is not translated to \r\n by the PTY.
        attrs = termios.tcgetattr(sys.stdout.fileno())
        attrs[1] &= ~termios.OPOST  # type: ignore[index]
        termios.tcsetattr(sys.stdout.fileno(), termios.TCSADRAIN, attrs)
    except Exception:
        pass


def _wait_for_pproxy(host: str, port: int, tries: int = 80, interval: float = 0.5) -> bool:
    """Poll until pproxy is accepting connections or we give up."""
    for _ in range(tries):
        try:
            s = socket.create_connection((host, port), timeout=1)
            s.close()
            return True
        except OSError:
            time.sleep(interval)
    return False


def main() -> None:
    global PROXY_HOST, PROXY_PORT
    if len(sys.argv) >= 2:
        PROXY_HOST = sys.argv[1]
    if len(sys.argv) >= 3:
        PROXY_PORT = int(sys.argv[2])

    _enter_raw_mode()

    # Wait for pproxy before signalling readiness (up to 80 × 0.5 s = 40 s).
    if not _wait_for_pproxy(PROXY_HOST, PROXY_PORT):
        sys.stdout.buffer.write(b"__CTERM_NOPROXY__\n")
        sys.stdout.buffer.flush()
        sys.exit(1)

    sys.stdout.buffer.write(b"__CTERM_READY__\n")
    sys.stdout.buffer.flush()

    fd = sys.stdin.fileno()
    # bytearray avoids O(n²) copying that b"" += chunk causes on every iteration.
    buf: bytearray = bytearray()

    while True:
        try:
            chunk = os.read(fd, 4096)
        except (OSError, EOFError):
            break
        if not chunk:
            break

        buf.extend(chunk)
        while b"\n" in buf:
            nl = buf.index(b"\n")
            line = bytes(buf[:nl]).rstrip(b"\r")
            buf = buf[nl + 1:]
            if not line:
                continue
            try:
                raw = base64.b64decode(line)
            except Exception:
                continue
            if len(raw) < 3:
                continue
            conn_id = int.from_bytes(raw[:2], "big")
            ftype = raw[2]
            payload = raw[3:]
            _handle_frame(conn_id, ftype, payload)


if __name__ == "__main__":
    main()
