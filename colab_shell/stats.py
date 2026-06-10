"""Runtime resource stats with live sparkline display.

Fetches RAM, disk, and GPU metrics from the Colab runtime's
``/api/colab/resources`` endpoint and renders them as unicode sparklines.

The sparkline history is maintained across ``--watch`` refreshes so the graph
grows over time rather than resetting on each poll.
"""

from __future__ import annotations

import collections
import sys
import time
from typing import Deque

from .client import ColabClient
from .utils import get_terminal_size

# Unicode block characters ordered from lowest to highest density.
_BLOCKS = " ▁▂▃▄▅▆▇█"
_HISTORY = 40  # max sparkline width (chars)

# ANSI helpers
_HIDE_CURSOR = "\x1b[?25l"
_SHOW_CURSOR = "\x1b[?25h"
_CLEAR_LINE = "\x1b[2K\r"
_CURSOR_UP = "\x1b[{}A"


def _spark_char(fraction: float) -> str:
    """Map a 0.0-1.0 fraction to a single sparkline block character."""
    idx = round(fraction * (len(_BLOCKS) - 1))
    return _BLOCKS[max(0, min(idx, len(_BLOCKS) - 1))]


def _sparkline(history: Deque[float]) -> str:
    """Render a deque of 0.0-1.0 values as a unicode sparkline string."""
    return "".join(_spark_char(v) for v in history)


def _fmt_bytes(n: int) -> str:
    """Human-readable byte size."""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024  # type: ignore[assignment]
    return f"{n:.1f} PB"


def _bar(label: str, used: float, total: float, history: Deque[float]) -> str:
    """Render one stat row: label  used/total  pct%  sparkline."""
    pct = used / total if total > 0 else 0.0
    history.append(pct)
    spark = _sparkline(history)
    cols, _ = get_terminal_size()
    label_w = 14
    lbl = label[:label_w].ljust(label_w)
    values = f"{_fmt_bytes(int(used)):>10} / {_fmt_bytes(int(total)):<10}  {pct*100:5.1f}%"
    line = f"  {lbl}  {values}  {spark}"
    return line[: cols - 1]


def render_stats(
    data: dict,
    histories: dict[str, Deque[float]],
) -> list[str]:
    """Turn a resources dict into a list of display lines (one per metric)."""
    lines: list[str] = []

    # -- RAM --
    mem = data.get("memory") or {}
    total_b = mem.get("totalBytes", 0)
    free_b = mem.get("freeBytes", 0)
    used_b = max(0, total_b - free_b)
    h_ram = histories.setdefault("ram", collections.deque(maxlen=_HISTORY))
    lines.append(_bar("RAM", used_b, total_b, h_ram))

    # -- Disk --
    for i, disk_entry in enumerate(data.get("disks") or []):
        fs = disk_entry.get("filesystem") or {}
        label = fs.get("label") or f"Disk {i}"
        d_total = fs.get("totalBytes", 0)
        d_used = fs.get("usedBytes", 0)
        h = histories.setdefault(f"disk_{i}", collections.deque(maxlen=_HISTORY))
        lines.append(_bar(label, d_used, d_total, h))

    # -- GPUs --
    for i, gpu in enumerate(data.get("gpus") or []):
        name = gpu.get("name") or f"GPU {i}"
        # GPU compute utilization (0-100 int from extension schema).
        gpu_util = (gpu.get("gpuUtilization") or 0) / 100.0
        h_util = histories.setdefault(f"gpu_{i}_util", collections.deque(maxlen=_HISTORY))
        h_util.append(gpu_util)
        spark_util = _sparkline(h_util)

        # GPU memory.
        gm_total = gpu.get("memoryTotalBytes", 0)
        gm_used = gpu.get("memoryUsedBytes", 0)
        h_gmem = histories.setdefault(f"gpu_{i}_mem", collections.deque(maxlen=_HISTORY))
        gpu_label = (name[:11] + " util").ljust(14)
        mem_label = (name[:10] + " vmem").ljust(14)

        gpu_pct = gpu_util * 100
        lines.append(
            f"  {gpu_label}  {'':>10}   {'':>10}   {gpu_pct:5.1f}%  {spark_util}"
        )
        lines.append(_bar(mem_label, gm_used, gm_total, h_gmem))

    return lines


class StatsDisplay:
    """Manages in-place redraws of the stats panel."""

    def __init__(self) -> None:
        self.histories: dict[str, Deque[float]] = {}
        self._last_line_count = 0

    def _erase_last(self) -> None:
        if self._last_line_count:
            # Move cursor up and clear each line.
            sys.stdout.write(_CURSOR_UP.format(self._last_line_count))
            for _ in range(self._last_line_count):
                sys.stdout.write(_CLEAR_LINE + "\n")
            sys.stdout.write(_CURSOR_UP.format(self._last_line_count))

    def show(self, data: dict, timestamp: float) -> None:
        lines = render_stats(data, self.histories)
        self._erase_last()
        ts = time.strftime("%H:%M:%S", time.localtime(timestamp))
        header = f"  Resource usage  ({ts})"
        sys.stdout.write(header + "\n")
        for line in lines:
            sys.stdout.write(line + "\n")
        self._last_line_count = 1 + len(lines)
        sys.stdout.flush()


def run_stats(client: ColabClient, watch: bool, interval: float) -> int:
    """Fetch and display stats; loop if ``watch`` is True."""
    display = StatsDisplay()
    try:
        sys.stdout.write(_HIDE_CURSOR)
        sys.stdout.flush()
        while True:
            try:
                data = client.get_resources()
            except (OSError, ValueError, KeyError) as exc:
                sys.stdout.write(_SHOW_CURSOR)
                print(f"[!] Failed to fetch resources: {exc}", file=sys.stderr)
                return 1
            display.show(data, time.time())
            if not watch:
                break
            time.sleep(interval)
    except KeyboardInterrupt:
        pass
    finally:
        sys.stdout.write(_SHOW_CURSOR)
        sys.stdout.flush()
    return 0
