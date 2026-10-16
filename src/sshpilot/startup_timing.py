"""Timestamped startup milestones for the GUI process.

Milestones go to the ``sshpilot.startup`` logger at DEBUG, so they appear with
``--verbose`` and nowhere else. Marks taken before logging is configured are
kept and replayed by :func:`logging_ready`, which ``setup_logging`` calls.

``SSHPILOT_STARTUP_PROFILE=<path>`` (used by ``scripts/startup_profile.py``)
additionally writes every milestone to ``<path>`` as JSON once the sidebar is
populated, and asks the application to quit.

Stdlib only: this module is imported before GTK.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time

logger = logging.getLogger("sshpilot.startup")

PROFILE_ENV = "SSHPILOT_STARTUP_PROFILE"
#: Wall-clock launch time (``time.time()``) a harness may pass in, so the
#: interpreter's own start-up shows up in the numbers.
LAUNCH_ENV = "SSHPILOT_STARTUP_LAUNCH_TIME"

_T0 = time.perf_counter()
_WALL0 = time.time()
_lock = threading.Lock()
_marks: list[tuple[str, float, str]] = []
_logging_ready = False


def _origin_offset_ms() -> float:
    """Milliseconds between the harness launch and this module's import."""
    raw = os.environ.get(LAUNCH_ENV)
    if not raw:
        return 0.0
    try:
        return max(0.0, (_WALL0 - float(raw)) * 1000.0)
    except ValueError:
        return 0.0


_OFFSET_MS = _origin_offset_ms()


def elapsed_ms() -> float:
    return _OFFSET_MS + (time.perf_counter() - _T0) * 1000.0


def mark(name: str, detail: str = "") -> None:
    """Record a milestone once; later marks of the same name are ignored."""
    at = elapsed_ms()
    with _lock:
        if any(existing == name for existing, _at, _detail in _marks):
            return
        _marks.append((name, at, detail))
        ready = _logging_ready
    if ready:
        _log(name, at, detail)


def _log(name: str, at: float, detail: str) -> None:
    logger.debug(
        "startup +%.0f ms %s%s", at, name, f" ({detail})" if detail else ""
    )


def logging_ready() -> None:
    """Replay the marks taken before logging existed, then log live."""
    global _logging_ready
    with _lock:
        if _logging_ready:
            return
        _logging_ready = True
        pending = list(_marks)
    for name, at, detail in pending:
        _log(name, at, detail)


def milestones() -> list[dict]:
    with _lock:
        return [
            {"name": name, "ms": round(at, 1), "detail": detail}
            for name, at, detail in _marks
        ]


def profile_path() -> str | None:
    return os.environ.get(PROFILE_ENV) or None


def write_profile() -> bool:
    """Write the milestones for a profiling harness; True when requested."""
    path = profile_path()
    if not path:
        return False
    try:
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"milestones": milestones()}, handle, indent=2)
    except OSError:
        logger.debug("Could not write the startup profile", exc_info=True)
    return True
