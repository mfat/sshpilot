"""Local shells a Windows local terminal tab can run.

``terminal.windows_shell`` in the config picks one: ``auto`` (the default:
PowerShell 7, then Windows PowerShell, then cmd), ``pwsh``, ``powershell``,
``cmd`` or ``wsl``. Only installed shells are offered.

GI-free; the frontend's local shell runner starts the command on a pseudo
console (``sshpilot.local_shell_pty``).
"""

from __future__ import annotations

import os
import shutil
from typing import Dict, List, Mapping, Optional

AUTO = "auto"
SHELL_ORDER = ("pwsh", "powershell", "cmd", "wsl")


def _system32(environ: Mapping[str, str]) -> str:
    return os.path.join(environ.get("SystemRoot") or environ.get("WINDIR") or r"C:\Windows", "System32")


def available_shells(environ: Optional[Mapping[str, str]] = None, which=shutil.which) -> Dict[str, List[str]]:
    """Installed shells as ``{id: argv}``, in preference order."""

    environ = os.environ if environ is None else environ
    path = environ.get("PATH") or environ.get("Path") or os.defpath
    system32 = _system32(environ)
    shells: Dict[str, List[str]] = {}
    pwsh = which("pwsh.exe", path=path) or which("pwsh", path=path)
    if pwsh:
        shells["pwsh"] = [pwsh, "-NoLogo"]
    powershell = os.path.join(system32, "WindowsPowerShell", "v1.0", "powershell.exe")
    if os.path.isfile(powershell):
        shells["powershell"] = [powershell, "-NoLogo"]
    comspec = environ.get("ComSpec") or os.path.join(system32, "cmd.exe")
    if os.path.isfile(comspec):
        shells["cmd"] = [comspec]
    wsl = os.path.join(system32, "wsl.exe")
    if os.path.isfile(wsl):
        shells["wsl"] = [wsl, "--cd", "~"]
    return shells


def shell_command(choice: Optional[str], environ: Optional[Mapping[str, str]] = None, which=shutil.which) -> List[str]:
    """The argv for ``choice`` (falling back to the best installed shell)."""

    shells = available_shells(environ, which)
    choice = (choice or AUTO).strip().lower()
    if choice in shells:
        return list(shells[choice])
    for name in SHELL_ORDER:
        if name in shells and name != "wsl":
            return list(shells[name])
    return ["cmd.exe"]


SHELL_LABELS = {
    AUTO: "Automatic",
    "pwsh": "PowerShell 7",
    "powershell": "Windows PowerShell",
    "cmd": "Command Prompt",
    "wsl": "WSL",
}
