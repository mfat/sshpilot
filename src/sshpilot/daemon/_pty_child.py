"""Exec-only helper that establishes a controlling terminal safely.

This module intentionally avoids ``subprocess`` and application imports.  It
runs in a fresh interpreter so the multithreaded daemon never needs a
``preexec_fn`` between fork and exec.
"""

from __future__ import annotations

import os
import sys


def main() -> int:
    try:
        separator = sys.argv.index("--")
        if separator != 3 or len(sys.argv) <= separator + 1:
            raise ValueError
        slave_fd = int(sys.argv[1])
        status_fd = int(sys.argv[2])
        argv = sys.argv[separator + 1 :]
        if slave_fd < 0 or status_fd < 0 or not argv:
            raise ValueError
        os.set_inheritable(status_fd, False)
        os.setsid()
        os.login_tty(slave_fd)
        os.execvpe(argv[0], argv, os.environ)
    except Exception:
        try:
            os.write(status_fd, b"launch_failed")
        except Exception:
            pass
        return 127


if __name__ == "__main__":
    raise SystemExit(main())
