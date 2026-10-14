"""Environment variables Windows programs need to run at all."""

from __future__ import annotations

import os
import sys


def disable_dbus_autolaunch(environ=None) -> bool:
    """Keep GLib from autolaunching a D-Bus session bus on Windows.

    GApplication registration connects to the session bus, and on Windows
    GLib autolaunches one (``gdbus.exe``) for it. If that bus wedges, for
    example after a process was killed mid-handshake, every later start
    blocks in ``register()`` with no timeout. sshPilot needs no session bus
    there, so it runs without one: each launch is its own primary instance
    (all of them share the daemon). An explicitly set address is kept.
    Returns True when it changed the environment.
    """

    environ = os.environ if environ is None else environ
    if sys.platform != "win32" or environ.get("DBUS_SESSION_BUS_ADDRESS"):
        return False
    environ["DBUS_SESSION_BUS_ADDRESS"] = "disabled:"
    return True

#: OpenSSH for Windows exits 255 with no output when these are missing:
#: it reads its system configuration from %PROGRAMDATA%\ssh, and the C
#: runtime and Winsock expect SystemRoot and friends. Windows environment
#: names are case-insensitive, so callers match them upper-cased.
WINDOWS_PROCESS_ENV = frozenset(
    {
        "SYSTEMROOT",
        "WINDIR",
        "SYSTEMDRIVE",
        "PROGRAMDATA",
        "PATHEXT",
        "COMSPEC",
        "TEMP",
        "TMP",
        "USERPROFILE",
        "USERNAME",
        "USERDOMAIN",
        "HOMEDRIVE",
        "HOMEPATH",
        "APPDATA",
        "LOCALAPPDATA",
    }
)
