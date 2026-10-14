"""Environment variables Windows programs need to run at all."""

from __future__ import annotations

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
