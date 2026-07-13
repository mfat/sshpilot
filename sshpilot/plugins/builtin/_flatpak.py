"""Shared helper for built-in protocol backends: resolve an external binary,
falling back to the Flatpak host when it isn't available inside the sandbox.

Built-ins ship in-app, so they may import app modules — but this keeps the
Flatpak detection in one place so telnet/docker/kubernetes/serial stay tidy.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from typing import List, Optional


def is_flatpak() -> bool:
    return bool(os.environ.get("FLATPAK_ID")) or os.path.exists("/.flatpak-info")


def resolve_host_binary(binary: str) -> Optional[List[str]]:
    """Return an argv *prefix* that runs ``binary``:

    * ``[<abs path>]`` when it's in the sandbox/PATH;
    * ``["flatpak-spawn", "--host", binary]`` when it's only on the Flatpak host;
    * ``None`` when it can't be found either way.

    Callers append their own arguments to the returned list. Only use this for
    commands that don't depend on in-app auth env (askpass/sshpass) — those
    can't cross the sandbox boundary unchanged.
    """
    found = shutil.which(binary)
    if found:
        return [found]
    if is_flatpak():
        spawn = shutil.which("flatpak-spawn")
        if spawn:
            try:
                result = subprocess.run(
                    [spawn, "--host", "which", binary],
                    capture_output=True, text=True, timeout=10, check=False)
                if result.returncode == 0:
                    return [spawn, "--host", binary]
            except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
                pass
    return None
