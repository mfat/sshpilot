"""Paramiko helpers for inspecting and walking remote SFTP trees."""

from __future__ import annotations

import errno
import os
from typing import Iterable, List, Tuple

import paramiko


def _sftp_path_exists(sftp: paramiko.SFTPClient, path: str) -> bool:
    """Return ``True`` if *path* exists on the remote SFTP server."""

    try:
        sftp.stat(path)
    except FileNotFoundError:
        return False
    except IOError as exc:
        error_code = getattr(exc, "errno", None)
        if error_code is None and exc.args:
            first_arg = exc.args[0]
            if isinstance(first_arg, int):
                error_code = first_arg
        if error_code in {errno.ENOENT, errno.EINVAL}:
            return False
        raise
    return True


def stat_isdir(attr: paramiko.SFTPAttributes) -> bool:
    """Return ``True`` when the attribute represents a directory."""

    return bool(attr.st_mode & 0o40000)


def walk_remote(
    sftp: paramiko.SFTPClient, root: str
) -> Iterable[Tuple[str, List[str], List[str]]]:
    """Yield a remote directory tree similar to :func:`os.walk`."""

    dirs: List[str] = []
    files: List[str] = []
    for entry in sftp.listdir_attr(root):
        if stat_isdir(entry):
            dirs.append(entry.filename)
        else:
            files.append(entry.filename)
    yield root, dirs, files
    for directory in dirs:
        new_root = os.path.join(root, directory)
        yield from walk_remote(sftp, new_root)
