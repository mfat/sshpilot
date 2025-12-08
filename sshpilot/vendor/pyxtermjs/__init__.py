"""Vendored copy of :mod:`pyxtermjs` used by sshPilot.

This package bundles upstream ``pyxtermjs`` so the application can
launch the PyXterm.js backend without requiring it to be installed in the
Python environment. The upstream project is distributed under the terms
of the MIT License; see :mod:`sshpilot.vendor.pyxtermjs.LICENSE` for
details.
"""

from __future__ import annotations

from .app import app, main, socketio  # noqa: F401

__all__ = ["app", "main", "socketio", "__version__", "VENDORED_VERSION"]

# The CLI exposes the version defined in app.py; we mirror it here so the
# vendored package behaves like the upstream distribution.
from .app import __version__ as __version__  # noqa: E402  (re-export)

# sshPilot documentation references this constant to indicate which
# upstream release is bundled.
VENDORED_VERSION = __version__
