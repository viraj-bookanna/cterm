"""Small shared helpers."""

from __future__ import annotations

import os
import sys

from .constants import XSS_PREFIX


def log(msg: str, end: str = "\n") -> None:
    print(f"[*] {msg}", end=end, flush=True)


def err(msg: str) -> None:
    print(f"[!] {msg}", file=sys.stderr, flush=True)


def strip_xss(text: str) -> str:
    """Colab API prefixes JSON responses with )]}' for XSS protection."""
    if text.startswith(XSS_PREFIX):
        return text[len(XSS_PREFIX) :]
    return text


def notebook_hash(notebook_id: str) -> str:
    """Transform a notebook UUID into Colab's ``nbh`` query parameter."""
    h = notebook_id.replace("-", "_")
    h += "." * max(0, 44 - len(h))
    return h


def get_terminal_size() -> tuple[int, int]:
    try:
        size = os.get_terminal_size()
        return size.columns, size.lines
    except OSError:
        return 80, 24
