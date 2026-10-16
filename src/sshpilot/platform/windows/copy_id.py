"""Public-key deployment on Windows, where OpenSSH has no ``ssh-copy-id``.

The built-in OpenSSH for Windows ships ``ssh``, ``scp`` and ``ssh-keygen``
but not ``ssh-copy-id`` (upstream it is a POSIX shell script). Git for Windows
carries the script, but running it needs its ``sh`` and MSYS userland, and
``shutil.which`` cannot resolve an extension-less script anyway.

So on Windows the daemon runs plain ``ssh`` with the same remote install
script ``ssh-copy-id`` sends: write to ``~/.ssh/authorized_keys`` (or
``/etc/dropbear/authorized_keys`` for root on OpenWrt), add a missing
trailing newline first, keep ``umask 077`` and restore the SELinux context
when ``restorecon`` exists. The key travels inside the remote command rather
than on stdin, so the launch keeps the same I/O as ``ssh-copy-id``.

The ``ssh-copy-id`` argv the launch provider builds only uses ``-F``, ``-p``
and ``-o`` before the target, all of which ``ssh`` accepts unchanged.
"""

from __future__ import annotations

import shlex
import shutil
import sys
from typing import List, Optional, Sequence

#: Printed by the remote script, mirroring ``ssh-copy-id``'s own summary.
ADDED_MESSAGE = "Number of key(s) added: 1"
SKIPPED_MESSAGE = "The key is already installed on the server; nothing was added."


def fallback_supported() -> bool:
    return sys.platform == "win32"


def resolve_ssh(path: Optional[str] = None) -> Optional[str]:
    """The ``ssh`` the fallback runs, or None off Windows / when missing."""
    if not fallback_supported():
        return None
    return shutil.which("ssh", path=path)


def _double_quote(text: str) -> str:
    """Quote *text* for a POSIX double-quoted string."""
    escaped = (
        text.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("$", "\\$")
        .replace("`", "\\`")
    )
    return f'"{escaped}"'


def read_public_key(public_key_path: str) -> str:
    """The single public-key line in *public_key_path*."""
    with open(public_key_path, encoding="utf-8") as handle:
        lines = [line.strip() for line in handle if line.strip()]
    if len(lines) != 1:
        raise ValueError("The public key file must contain exactly one key")
    key = lines[0]
    if len(key.split()) < 2 or any(ch in key for ch in "\r\n\x00"):
        raise ValueError("The public key file does not contain a public key")
    return key


def install_command(public_key: str, *, force: bool = False) -> str:
    """The remote command that appends *public_key* to authorized_keys.

    Without *force* a key whose base64 body is already present is skipped,
    standing in for ``ssh-copy-id``'s check of which keys already log in.
    """
    key_body = public_key.split()[1]
    key = _double_quote(public_key)
    skip_existing = (
        ""
        if force
        else (
            f'grep -qF {_double_quote(key_body)} "$AUTH_KEY_FILE" 2>/dev/null && '
            f"{{ echo {_double_quote(SKIPPED_MESSAGE)}; exit 0; }}; "
        )
    )
    script = (
        "cd; umask 077; "
        'AUTH_KEY_FILE=".ssh/authorized_keys"; '
        "[ -f /etc/openwrt_release ] && "
        '{ [ "$LOGNAME" = "root" ] || [ "$(id -u)" = "0" ]; } && '
        "AUTH_KEY_FILE=/etc/dropbear/authorized_keys; "
        '[ "$(uname -s)" = "Haiku" ] && '
        "AUTH_KEY_FILE=config/settings/ssh/authorized_keys; "
        'AUTH_KEY_DIR=$(dirname "$AUTH_KEY_FILE"); '
        'mkdir -p "$AUTH_KEY_DIR" || exit 1; '
        f"{skip_existing}"
        '{ [ -z "$(tail -c1 "$AUTH_KEY_FILE" 2>/dev/null)" ] || '
        'echo >> "$AUTH_KEY_FILE" || exit 1; } && '
        f'printf "%s\\n" {key} >> "$AUTH_KEY_FILE" || exit 1; '
        "if type restorecon >/dev/null 2>&1; then "
        'restorecon -F "$AUTH_KEY_DIR" "$AUTH_KEY_FILE"; fi; '
        f"echo {_double_quote(ADDED_MESSAGE)}"
    )
    # Like ssh-copy-id: 'exec sh -c' gets a POSIX shell whatever the login
    # shell is.
    return f"exec sh -c {shlex.quote(script)}"


def ssh_argv_from_copy_id(
    copy_id_argv: Sequence[str],
    ssh_executable: str,
    public_key: str,
    *,
    force: bool = False,
) -> List[str]:
    """Turn the provider's ``ssh-copy-id`` argv into an ``ssh`` argv.

    Drops ``-f`` and ``-i <key>`` (ssh-copy-id's own flags), keeps every
    other option and the trailing target, and appends the install command.
    """
    args = list(copy_id_argv[1:])
    if not args:
        raise ValueError("The ssh-copy-id command has no target")
    target = args.pop()
    options: List[str] = []
    index = 0
    while index < len(args):
        arg = args[index]
        if arg in ("-o", "-p", "-F"):
            options.extend(args[index:index + 2])
            index += 2
            continue
        if arg == "-f":
            index += 1
            continue
        if arg == "-i":
            index += 2
            continue
        options.append(arg)
        index += 1
    return [
        ssh_executable,
        *options,
        "-o",
        "RequestTTY=no",
        "--",
        target,
        install_command(public_key, force=force),
    ]
