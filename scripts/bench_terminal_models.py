#!/usr/bin/env python3
"""Feed-throughput benchmark for the built-in terminal's emulator models.

Feeds two 1.5 MB workloads in 4 KiB chunks (a typical PTY read) into an
80x24 model with 10 000 lines of scrollback and reports MB/s, plus the cost of
reading every screen row once (what one redraw of the GTK view does).

* plain: 79 printable ASCII characters + CRLF per line.
* sgr:   the same text with every word wrapped in SGR (bold/italic/16-colour,
         256-colour and truecolour foregrounds and backgrounds, then reset).

Usage: PYTHONPATH=src python scripts/bench_terminal_models.py [pyte] [vterm]
"""

from __future__ import annotations

import sys
import time

SIZE = 1_500_000
CHUNK = 4096
WORDS = "lorem ipsum dolor sit amet consectetur adipiscing elit sed do eiusmod".split()


def plain_workload() -> bytes:
    line = (" ".join(WORDS) * 2)[:79].encode() + b"\r\n"
    return (line * (SIZE // len(line) + 1))[:SIZE]


def sgr_workload() -> bytes:
    styles = [
        b"\x1b[1;31m", b"\x1b[3;32m", b"\x1b[4;33;44m", b"\x1b[7;35m",
        b"\x1b[38;5;208m", b"\x1b[48;5;24;38;5;231m",
        b"\x1b[38;2;250;128;114m", b"\x1b[48;2;20;40;60;1m",
    ]
    parts = []
    for index, word in enumerate(WORDS * 2):
        parts.append(styles[index % len(styles)] + word.encode() + b"\x1b[0m")
    line = b" ".join(parts) + b"\r\n"
    return (line * (SIZE // len(line) + 1))[:SIZE]


def make(name: str):
    if name == "pyte":
        from sshpilot.pyte_terminal_model import TerminalModel

        return TerminalModel(80, 24, scrollback_limit=10000)
    from sshpilot.vterm_terminal_model import VtermTerminalModel

    return VtermTerminalModel(80, 24, scrollback_limit=10000)


def bench(name: str, data: bytes, repeats: int) -> tuple:
    best = float("inf")
    for _ in range(repeats):
        model = make(name)
        start = time.perf_counter()
        for offset in range(0, len(data), CHUNK):
            model.feed(data[offset:offset + CHUNK])
        best = min(best, time.perf_counter() - start)
    # One full redraw's worth of row reads (cold row cache).
    model.feed(b"\x1b[H")
    start = time.perf_counter()
    top = model.history_length
    for row in range(top, top + model.lines):
        line = model.line(row)
        for column in range(model.columns):
            line.get(column)
    redraw_ms = (time.perf_counter() - start) * 1000
    return len(data) / best / 1e6, redraw_ms


def main(argv) -> None:
    names = argv or ["pyte", "vterm"]
    workloads = {"plain": plain_workload(), "sgr": sgr_workload()}
    print(f"python {sys.version.split()[0]}, {SIZE / 1e6:.1f} MB per workload, {CHUNK} B chunks")
    for name in names:
        repeats = 1 if name == "pyte" else 5
        for label, data in workloads.items():
            rate, redraw = bench(name, data, repeats)
            print(f"{name:6} {label:6} {rate:8.2f} MB/s   row read for one redraw: {redraw:6.2f} ms")


if __name__ == "__main__":
    main(sys.argv[1:])
