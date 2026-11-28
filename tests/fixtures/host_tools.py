"""Run test-rig programs on the host when the tests run inside the Flatpak.

``scripts/run-flatpak-gui-tests.sh`` runs the GUI tests inside the installed
Flatpak, so the app under test is the one users get: its Python modules, its
bundled ssh and mosh, and its ``flatpak-spawn --host`` route to docker, kubectl,
telnet and picocom. The rigs around the app (container runtimes, socat,
kubectl for setup) are not in the sandbox, so they run on the host the same way
the app's own host programs do. Nothing else changes: the sandbox shares the
host's network, and the script mounts its work directory at the same path on
both sides, so ports and file paths mean the same thing everywhere.

Outside a Flatpak every helper here is a plain ``subprocess`` call.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from typing import Mapping, Optional, Sequence

IN_FLATPAK = os.path.exists("/.flatpak-info")

_SPAWN = ("flatpak-spawn", "--host", "--watch-bus")


def _on_host(program: str) -> bool:
    """True when ``program`` has to run on the host: inside a Flatpak, a bare
    name the sandbox does not have."""
    return IN_FLATPAK and os.sep not in program and shutil.which(program) is None


def which(program: str) -> Optional[str]:
    """What to run ``program`` as, or None when neither side has it."""
    found = shutil.which(program)
    if found or not IN_FLATPAK:
        return found
    try:
        probe = subprocess.run(
            [*_SPAWN, "sh", "-c", 'command -v "$1"', "sh", program],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return program if probe.returncode == 0 else None


def _env_changes(env: Optional[Mapping[str, str]]) -> list[str]:
    """flatpak-spawn options for what ``env`` changes from this process's.

    A host program starts from the host session's environment: flatpak-spawn
    forwards none of the sandbox's. What a caller sets or removes on purpose
    (see temporary_openssh._container_env: no session bus for podman) is
    carried over as explicit changes.
    """
    if env is None:
        return []
    options = [f"--unset-env={name}" for name in os.environ if name not in env]
    options += [
        f"--env={name}={value}"
        for name, value in env.items()
        if os.environ.get(name) != value
    ]
    return options


def command(argv: Sequence[str], env: Optional[Mapping[str, str]] = None) -> list[str]:
    """``argv``, prefixed with ``flatpak-spawn --host`` when it runs there."""
    argv = [str(part) for part in argv]
    if argv and _on_host(argv[0]):
        return [*_SPAWN, *_env_changes(env), *argv]
    return argv


def _call(argv: Sequence[str], kwargs: dict) -> tuple[list[str], dict]:
    if argv and _on_host(str(argv[0])):
        # flatpak-spawn itself needs this process's environment (the sandbox
        # bus address); the caller's goes to the host program.
        kwargs = dict(kwargs)
        return command(argv, kwargs.pop("env", None)), kwargs
    return command(argv), kwargs


def run(argv: Sequence[str], **kwargs) -> subprocess.CompletedProcess:
    full, kwargs = _call(argv, kwargs)
    return subprocess.run(full, **kwargs)


def popen(argv: Sequence[str], **kwargs) -> subprocess.Popen:
    """Start ``argv``; terminating the result stops the host program too
    (flatpak-spawn forwards the signal, and --watch-bus ends it with us)."""
    full, kwargs = _call(argv, kwargs)
    return subprocess.Popen(full, **kwargs)
