"""Exec-only helper that establishes a controlling terminal safely.

In a source/venv run this module is invoked as a standalone script
(``sys.executable _pty_child.py <slave_fd> <status_fd> -- <ssh argv>``) so it
runs in a fresh interpreter and the multithreaded daemon never needs a
``preexec_fn`` between fork and exec. In a frozen (PyInstaller) build,
``sys.executable`` is the application binary itself, not a Python
interpreter, so ``run.py`` dispatches an internal flag to this same ``main``
instead — see ``PtySessionProcessRunner.start`` in ``pty_runner.py``. This
module still intentionally avoids ``subprocess`` and application imports so
either invocation shape stays cheap and side-effect free right up to exec.
"""

from __future__ import annotations

import os
import sys
from typing import Optional, Sequence


def main(argv: Optional[Sequence[str]] = None) -> int:
    if argv is None:
        argv = sys.argv
    try:
        separator = argv.index("--")
        if separator != 3 or len(argv) <= separator + 1:
            raise ValueError
        slave_fd = int(argv[1])
        status_fd = int(argv[2])
        ssh_argv = argv[separator + 1 :]
        if slave_fd < 0 or status_fd < 0 or not ssh_argv:
            raise ValueError
        os.set_inheritable(status_fd, False)
        os.setsid()
        os.login_tty(slave_fd)
        os.execvpe(ssh_argv[0], ssh_argv, os.environ)
    except Exception:
        try:
            os.write(status_fd, b"launch_failed")
        except Exception:
            pass
        return 127


if __name__ == "__main__":
    raise SystemExit(main())
