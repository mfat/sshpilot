"""Make the built-in OpenSSH for Windows the one sshPilot runs.

Windows can carry several OpenSSH builds on PATH: the built-in one in
``System32\\OpenSSH`` and MSYS-based copies from Git for Windows, MSYS2 or
Cygwin. Those use a different agent (a Unix-socket emulation instead of the
``\\\\.\\pipe\\openssh-ssh-agent`` service), different config and key paths,
and cannot run the ``askpass.cmd`` launcher. Git for Windows commonly lands
*ahead* of System32\\OpenSSH, so a plain ``ssh`` would silently pick its copy,
and ``ssh`` and ``ssh-add`` could even come from different builds.

Both the GUI and the daemon (which inherits the GUI's environment) resolve
plain ``ssh``/``ssh-add``/``ssh-keygen``, so putting the built-in directory
first on PATH, once at startup, keeps every child on one consistent build.
"""

from __future__ import annotations

import os
import sys
from typing import MutableMapping, Optional


def builtin_openssh_dir(environ: Optional[MutableMapping[str, str]] = None) -> str:
    env = os.environ if environ is None else environ
    root = env.get("SystemRoot") or env.get("SYSTEMROOT") or r"C:\Windows"
    return os.path.join(root, "System32", "OpenSSH")


def prefer_builtin_openssh(environ: Optional[MutableMapping[str, str]] = None) -> bool:
    """Move System32\\OpenSSH to the front of PATH. True when PATH changed.

    No-op off Windows, or when the built-in client is not installed.
    """

    if sys.platform != "win32":
        return False
    env = os.environ if environ is None else environ
    directory = builtin_openssh_dir(env)
    if not os.path.isfile(os.path.join(directory, "ssh.exe")):
        return False
    entries = [entry for entry in env.get("PATH", "").split(os.pathsep) if entry]
    normalized = os.path.normcase(os.path.normpath(directory))
    rest = [e for e in entries if os.path.normcase(os.path.normpath(e)) != normalized]
    updated = os.pathsep.join([directory, *rest])
    if updated == env.get("PATH", ""):
        return False
    env["PATH"] = updated
    return True
