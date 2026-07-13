"""Vendored xterm.js assets for sshPilot's embedded terminal.

The old pyxtermjs Flask/WebSocket server was removed once the terminal moved to an
in-process PTY bridge (see :mod:`sshpilot.xterm_pty_bridge` and
:mod:`sshpilot.xterm_shell`). Only the ``xterm/`` asset directory remains here; it
is read from disk by ``xterm_shell.asset_dir()`` (system ``libjs-xterm`` is
preferred at runtime, this bundled copy is the fallback).
"""
