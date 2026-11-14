#!/usr/bin/env python3
"""Steady-state cost of "output arrives, the view redraws" per emulator model.

Repeats: feed one 4 KiB chunk of a workload, then read every visible row (what
one repaint of the built-in GTK view does), on a 120x40 model that has
already run for a while. Reports the median and 95th percentile per cycle.
Workloads are the ones in bench_terminal_models.py.

Usage: PYTHONPATH=src python scripts/bench_terminal_redraw.py [pyte] [vterm] [ghostty]
"""

from __future__ import annotations

import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import bench_terminal_models as workloads  # noqa: E402

COLUMNS, LINES = 120, 40
CHUNK = 4096
CYCLES = 200


def make(name: str):
    if name == "pyte":
        from sshpilot.pyte_terminal_model import TerminalModel as Model
    elif name == "ghostty":
        from sshpilot.ghostty_terminal_model import GhosttyTerminalModel as Model
    else:
        from sshpilot.vterm_terminal_model import VtermTerminalModel as Model
    return Model(COLUMNS, LINES, scrollback_limit=10000)


def run(name: str, data: bytes) -> tuple:
    model = make(name)
    chunks = [data[offset:offset + CHUNK] for offset in range(0, len(data), CHUNK)]
    for chunk in chunks[:50]:  # warm up: history and caches as in a live tab
        model.feed(chunk)
    timings = []
    for index in range(CYCLES):
        chunk = chunks[(50 + index) % len(chunks)]
        start = time.perf_counter()
        model.feed(chunk)
        top = model.history_length
        for row in range(top, top + model.lines):
            line = model.line(row)
            for column in range(model.columns):
                line.get(column)
        timings.append(time.perf_counter() - start)
    timings.sort()
    return statistics.median(timings) * 1000, timings[int(len(timings) * 0.95)] * 1000


def main(argv) -> None:
    names = argv or ["vterm", "ghostty"]
    data = {"plain": workloads.plain_workload(), "sgr": workloads.sgr_workload()}
    print(f"python {sys.version.split()[0]}, {COLUMNS}x{LINES}, {CHUNK} B per cycle, {CYCLES} cycles")
    for name in names:
        for label, workload in data.items():
            median, p95 = run(name, workload)
            print(f"{name:7} {label:6} feed + full redraw: median {median:6.2f} ms, p95 {p95:6.2f} ms")


if __name__ == "__main__":
    main(sys.argv[1:])
