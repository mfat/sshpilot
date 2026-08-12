#!/usr/bin/env python3
"""
Simple runner for the simplified sshpilot package under new/
"""

import sys
import os

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PARENT = os.path.dirname(CURRENT_DIR)
SRC_DIR = os.path.join(CURRENT_DIR, "src")

# Ensure the package is importable before dispatching any headless entrypoint.
# These branches must run before importing sshpilot.main, which imports GTK.
sys.path.insert(0, CURRENT_DIR)
sys.path.insert(0, PARENT)
if os.path.isdir(SRC_DIR):
    sys.path.insert(0, SRC_DIR)

# Intercept --askpass BEFORE importing sshpilot.main (which has module-level GTK imports).
# SSH calls the askpass wrapper with the prompt as the first argument; we re-invoke
# ourselves here so keyring/libsecret are available in our own Python environment.
if len(sys.argv) > 1 and sys.argv[1] == '--askpass':
    from sshpilot.askpass_utils import run_askpass_and_write
    prompt = sys.argv[2] if len(sys.argv) > 2 else ""
    sys.exit(run_askpass_and_write(prompt))

if len(sys.argv) > 1 and sys.argv[1] == '--daemon':
    from sshpilot.daemon.cli import main as daemon_main

    sys.exit(daemon_main(sys.argv[2:]))

from sshpilot.main import main

if __name__ == '__main__':
    main()
