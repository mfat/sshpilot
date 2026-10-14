#!/bin/bash
# Run sshPilot from a source checkout on Windows (MSYS2 UCRT64 shell).
# One-time setup: docs/running-on-windows.md. Extra arguments go to run.py.
set -e
cd "$(dirname "$0")/.."
if [ "${MSYSTEM:-}" != "UCRT64" ]; then
  echo "Run this from the MSYS2 UCRT64 shell." >&2
  exit 1
fi
if [ ! -x .venv-msys/bin/python ]; then
  python -m venv --system-site-packages .venv-msys
  .venv-msys/bin/python -m pip install pyte keyring certifi pykeepass
fi
exec .venv-msys/bin/python run.py "$@"
