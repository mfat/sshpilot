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

from sshpilot.main import main

if __name__ == '__main__':
    main()
