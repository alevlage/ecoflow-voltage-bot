"""Voltage history: the last 3 hours in memory, a CSV log, and a chart.

Each row is `timestamp,volts,watts`. Volts are left empty while there's no
electricity, because the device keeps reporting a stale voltage then.

Charts are drawn from memory only. The CSV is kept as a full log and read
once at startup, and then only its end, to refill the last 3 hours.
"""

from __future__ import annotations

import csv
import io
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path

import matplotlib

matplotlib.use("Agg")  # no display; render to PNG only
import matplotlib.dates as mdates
import matplotlib.pyplot as plt

LOG_PATH = Path(__file__).with_name("voltage_log.csv")
NOMINAL_VOLTS = 230
KEEP_MINUTES = 180
# 3 hours at up to one row per second is ~240 KB; read a bit more than that.
TAIL_BYTES = 512 * 1024

_recent: deque[tuple[float, float | None]] = deque()  # (timestamp, volts or None)
_lock = threading.Lock()  # append and plot run in different threads


def _prune(now: float) -> None:
    cutoff = now - KEEP_MINUTES * 60
    while _recent and _recent[0][0] < cutoff:
        _recent.popleft()


def append(volts: float | None, on: bool, watts: float | None = None, ts: float | None = None) -> None:
    ts = ts or time.time()
    volts = round(volts, 1) if on and volts is not None else None
    with LOG_PATH.open("a", newline="") as f:
        csv.writer(f).writerow([
            f"{ts:.0f}",
            f"{volts:.1f}" if volts is not None else "",
            f"{watts:.0f}" if watts is not None else "",
        ])
    with _lock:
        _recent.append((ts, volts))
        _prune(ts)


def load_recent() -> None:
    """Refill memory with the last 3 hours from the end of the CSV."""
    if not LOG_PATH.exists():
        return
    with LOG_PATH.open("rb") as f:
        f.seek(0, 2)
        size = f.tell()
        f.seek(max(0, size - TAIL_BYTES))
        tail = f.read().decode(errors="ignore").splitlines()
    if size > TAIL_BYTES:
        tail = tail[1:]  # first line is probably cut in half
    cutoff = time.time() - KEEP_MINUTES * 60
    with _lock:
        _recent.clear()
        for ts, volts, *_ in csv.reader(tail):
            if float(ts) >= cutoff:
                _recent.append((float(ts), float(volts) if volts else None))


def recent(since: float) -> list[tuple[float, float | None]]:
    """Readings newer than `since`, oldest first, as (timestamp, volts or None)."""
    with _lock:
        return [row for row in _recent if row[0] > since]


def plot_png(minutes: int) -> bytes | None:
    """Render the last `minutes` (up to 180) of history. Returns PNG bytes, or None if empty."""
    minutes = min(minutes, KEEP_MINUTES)
    since = time.time() - minutes * 60
    with _lock:
        rows = [row for row in _recent if row[0] >= since]
    if not rows:
        return None
    times = [datetime.fromtimestamp(ts) for ts, _ in rows]
    volts = [v if v is not None else float("nan") for _, v in rows]

    fig, ax = plt.subplots(figsize=(9, 4.5), dpi=120)
    ax.plot(times, volts, color="#1f77b4", linewidth=1.4)
    ax.axhline(NOMINAL_VOLTS, color="#888", linestyle="--", linewidth=0.8, label=f"{NOMINAL_VOLTS} V")
    # Shade stretches with no electricity.
    outage_start = None
    outage_label = "No electricity"
    for t, v in zip(times + [None], volts + [0.0]):
        off = v != v  # NaN
        if off and outage_start is None:
            outage_start = t
        elif not off and outage_start is not None:
            ax.axvspan(outage_start, t or times[-1], color="#d62728", alpha=0.15, label=outage_label)
            outage_label = None  # one legend entry for all outages
            outage_start = None
    known = [v for v in volts if v == v]
    now = f"now {volts[-1]:.1f} V" if volts[-1] == volts[-1] else "now no electricity"
    if known:
        ax.set_title(f"AC input voltage, last {minutes} min — {now}, "
                     f"min {min(known):.1f}, max {max(known):.1f}")
    else:
        ax.set_title(f"No electricity in the last {minutes} min")
    ax.set_ylabel("Volts")
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
    ax.grid(alpha=0.3)
    ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.12), ncol=2, frameon=False)
    fig.autofmt_xdate()
    fig.tight_layout()

    buf = io.BytesIO()
    fig.savefig(buf, format="png")
    plt.close(fig)
    return buf.getvalue()
