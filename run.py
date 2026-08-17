#!/usr/bin/env python3
"""
Simple runner for the simplified sshpilot package under new/
"""

import sys
import os

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PARENT = os.path.dirname(CURRENT_DIR)
SRC_DIR = os.path.join(CURRENT_DIR, "src")

# Ensure the package is importable before dispatching the daemon entrypoint.
# The daemon owns all production SSH askpass handling; this runner has no
# frontend askpass or local SSH fast path.
sys.path.insert(0, CURRENT_DIR)
sys.path.insert(0, PARENT)
if os.path.isdir(SRC_DIR):
    sys.path.insert(0, SRC_DIR)

if len(sys.argv) > 1 and sys.argv[1] == '--daemon':
    from sshpilot.daemon.cli import main as daemon_main

    sys.exit(daemon_main(sys.argv[2:]))

# In a frozen (PyInstaller) build, sys.executable is this application binary,
# not a Python interpreter, so subprocess spawns that expect to run a small
# helper script under sys.executable (the PTY exec helper, the askpass
# helpers) must instead re-invoke this same binary with one of the internal
# flags below. Keep these checks — and their imports — ahead of the GTK
# import below: they must stay cheap, since they sit on the connect path.
if len(sys.argv) > 1 and sys.argv[1] == '--internal-pty-child':
    from sshpilot.daemon._pty_child import main as pty_child_main

    # _pty_child.main() expects argv[0] to be an ignored "script name" slot
    # followed by <slave_fd> <status_fd> -- <ssh argv>; sys.argv[1:] (which
    # drops only the executable path, keeping this flag as that slot) already
    # has that shape.
    sys.exit(pty_child_main(sys.argv[1:]))

if len(sys.argv) > 1 and sys.argv[1] == '--internal-askpass':
    from sshpilot.daemon.askpass_helper import main as daemon_askpass_main

    # daemon.askpass_helper.main() reads the prompt from sys.argv[1]; drop
    # this flag so that index lines up with a direct script invocation.
    sys.argv = sys.argv[1:]
    sys.exit(daemon_askpass_main())

if len(sys.argv) > 1 and sys.argv[1] == '--internal-native-askpass':
    from sshpilot.askpass_utils import run_askpass_and_write

    prompt = sys.argv[2] if len(sys.argv) > 2 else ''
    sys.exit(run_askpass_and_write(prompt))

from sshpilot.main import main

if __name__ == '__main__':
    main()
