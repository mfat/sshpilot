#!/usr/bin/env python3
"""
Simple runner for the simplified sshpilot package under new/
"""

import argparse
import os
import sys
from typing import List, Tuple

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PARENT = os.path.dirname(CURRENT_DIR)
SRC_DIR = os.path.join(CURRENT_DIR, "src")

# Ensure simplified package is importable
sys.path.insert(0, CURRENT_DIR)
sys.path.insert(0, PARENT)
if os.path.isdir(SRC_DIR):
    sys.path.insert(0, SRC_DIR)


def _parse_ui_choice(argv: List[str]) -> Tuple[str, List[str]]:
    """Return the requested UI backend and the remaining arguments."""
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument(
        "--ui",
        choices=["gtk", "qt"],
        help="Choose which UI backend to launch (experimental Qt preview available)",
    )
    args, remaining = parser.parse_known_args(argv)

    env_choice = os.environ.get("SSHPILOT_UI", "").strip().lower() or None
    choice = args.ui or env_choice or "gtk"
    if choice not in {"gtk", "qt"}:
        choice = "gtk"

    return choice, remaining


def main() -> int:
    ui_choice, remaining = _parse_ui_choice(sys.argv[1:])
    sys.argv = [sys.argv[0], *remaining]

    if ui_choice == "qt":
        from sshpilot_qt.app import main as qt_main

        return qt_main()

    from sshpilot.main import main as gtk_main

    return gtk_main()


if __name__ == '__main__':
    main()
