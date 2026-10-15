"""Small process helpers whose correct form differs on Windows.

Each helper keeps POSIX behaviour exactly as the call sites had it; only the
Windows branch is shared. See docs/development/windows-compatibility.md.
"""
from __future__ import annotations

import os
import subprocess
import sys
from typing import Any, Dict, TypeVar, Union

_T = TypeVar("_T")

def hidden_console_kwargs() -> Dict[str, Any]:
    """``subprocess`` keyword arguments that keep a child's console hidden.

    The windowed (frozen) GUI build has no console, so a console program it
    starts gets a new, visible one that flashes on screen. Pass these to
    ``run`` / ``Popen`` for helper processes started from the GUI process.
    Do not use them where a console window *is* the point (the external
    terminal launch uses ``CREATE_NEW_CONSOLE``), and do not combine them with
    other ``creationflags``.

    Follows the real interpreter, not ``sys.platform`` (which tests patch):
    POSIX ``Popen`` rejects any non-zero ``creationflags``.
    """
    if hasattr(subprocess, "CREATE_NO_WINDOW"):
        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    return {}


def pid_alive(pid: int, *, on_error: _T) -> Union[bool, _T]:
    """Whether a process holds *pid* (identity unknown).

    ``os.kill(pid, 0)`` is not a probe on Windows: Python calls
    ``TerminateProcess`` for any signal other than the console CTRL events.
    Windows asks psutil instead.

    *on_error* is the answer when the probe itself fails (a permission error,
    an unexpected ``OSError``, psutil missing). Callers differ on purpose:
    some must treat an unknown pid as alive (never reuse or forget it on
    doubt), others as gone, and some neither (pass ``None`` and leave the pid
    alone). A dead pid is always ``False``.
    """
    if sys.platform == "win32":
        try:
            import psutil

            return bool(psutil.pid_exists(pid))
        except Exception:
            return on_error
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return on_error
    return True


def windows_cmd_command_line(command: str) -> str:
    """The command line that runs *command* under ``%COMSPEC% /d /s /c``.

    Returned as one string, not an argv list: ``subprocess`` would run
    ``list2cmdline`` over a list and turn every ``"`` in *command* into
    ``\\"``, which ``cmd`` does not understand. ``/s`` strips exactly the
    outer pair of quotes added here.
    """
    comspec = (os.environ.get("COMSPEC") or "cmd.exe").strip('"')
    return f'"{comspec}" /d /s /c "{command}"'
