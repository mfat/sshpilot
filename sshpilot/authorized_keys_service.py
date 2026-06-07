"""Async service for loading and saving a remote ``~/.ssh/authorized_keys`` file.

Sits on top of :class:`AsyncSFTPManager` from ``file_manager_window`` and
returns futures so callers can dispatch back to the GTK main thread via the
existing pattern.
"""

from __future__ import annotations

import logging
import os
import posixpath
import time
from concurrent.futures import Future
from typing import List

from .authorized_keys_parser import Item, parse_file, serialize

logger = logging.getLogger(__name__)


REMOTE_BASENAME = "authorized_keys"
SSH_DIR_BASENAME = ".ssh"


class AuthorizedKeysService:
    """Load / save the remote authorized_keys file via an AsyncSFTPManager."""

    def __init__(self, sftp_manager) -> None:
        self._manager = sftp_manager
        self._backup_done = False

    # -- path helpers ---------------------------------------------------

    def _resolve_paths(self, sftp) -> tuple[str, str, str]:
        """Return (home, ssh_dir, authorized_keys_path) as remote POSIX paths."""
        home = sftp.normalize(".")
        ssh_dir = posixpath.join(home, SSH_DIR_BASENAME)
        ak_path = posixpath.join(ssh_dir, REMOTE_BASENAME)
        return home, ssh_dir, ak_path

    # -- load -----------------------------------------------------------

    def load(self) -> Future:
        """Read and parse ``~/.ssh/authorized_keys``. Missing file -> empty list."""

        def _do() -> List[Item]:
            sftp = self._manager._sftp
            if sftp is None:
                raise RuntimeError("SFTP session is not connected")
            _, ssh_dir, ak_path = self._resolve_paths(sftp)
            try:
                sftp.stat(ssh_dir)
            except IOError:
                logger.debug("Remote ~/.ssh does not exist yet")
                return []
            try:
                with sftp.file(ak_path, "r") as fh:
                    data = fh.read()
            except IOError:
                logger.debug("Remote authorized_keys does not exist yet")
                return []
            if isinstance(data, bytes):
                text = data.decode("utf-8", errors="replace")
            else:
                text = data
            return parse_file(text)

        return self._manager._submit(_do)

    # -- save -----------------------------------------------------------

    def save(self, items: List[Item], *, make_backup: bool = True) -> Future:
        """Atomically write items back. Returns a future resolving when done.

        - Ensures ``~/.ssh`` exists with mode 0700.
        - On first save in this session (when ``make_backup`` true), renames
          the existing file to ``authorized_keys.bak-<unix-ts>``.
        - Writes to a temp sibling, then ``posix_rename`` over the target.
        - chmods the target to 0600.
        """
        serialized = serialize(items)
        do_backup = make_backup and not self._backup_done

        def _do() -> None:
            sftp = self._manager._sftp
            ssh = self._manager._client
            if sftp is None or ssh is None:
                raise RuntimeError("SFTP session is not connected")
            _, ssh_dir, ak_path = self._resolve_paths(sftp)

            # Ensure ~/.ssh exists with correct mode. mkdir -p is harmless.
            quoted = _shell_quote(ssh_dir)
            _, stdout, stderr = ssh.exec_command(
                f"mkdir -p {quoted} && chmod 700 {quoted}"
            )
            exit_status = stdout.channel.recv_exit_status()
            if exit_status != 0:
                err = stderr.read().decode("utf-8", errors="replace")
                raise IOError(f"Failed to ensure {ssh_dir}: {err.strip()}")

            # Backup existing file if requested and it exists.
            if do_backup:
                try:
                    sftp.stat(ak_path)
                    backup_path = f"{ak_path}.bak-{int(time.time())}"
                    # posix_rename overwrites; use it to keep the move atomic.
                    sftp.posix_rename(ak_path, backup_path)
                    logger.info("Backed up authorized_keys to %s", backup_path)
                except IOError:
                    # No existing file to back up.
                    pass

            tmp_path = ak_path + ".sshpilot.tmp"
            payload = serialized.encode("utf-8")
            with sftp.file(tmp_path, "w") as fh:
                fh.write(payload)
            try:
                sftp.chmod(tmp_path, 0o600)
            except IOError as exc:
                logger.debug("chmod on tmp failed (continuing): %s", exc)
            sftp.posix_rename(tmp_path, ak_path)
            sftp.chmod(ak_path, 0o600)

        future = self._manager._submit(_do)

        def _mark_backup_done(fut: Future) -> None:
            if fut.exception() is None and do_backup:
                self._backup_done = True

        future.add_done_callback(_mark_backup_done)
        return future


def _shell_quote(s: str) -> str:
    """Minimal POSIX shell single-quoting for paths."""
    return "'" + s.replace("'", "'\\''") + "'"


class LocalAuthorizedKeysService:
    """Same interface as :class:`AuthorizedKeysService`, but operates on a local file."""

    def __init__(self, path: str) -> None:
        self.path = os.path.expanduser(path)
        self._backup_done = False

    def _resolved_future(self, value=None, exc=None) -> Future:
        fut: Future = Future()
        if exc is not None:
            fut.set_exception(exc)
        else:
            fut.set_result(value)
        return fut

    def load(self) -> Future:
        try:
            ssh_dir = os.path.dirname(self.path)
            if not os.path.isdir(ssh_dir):
                return self._resolved_future([])
            if not os.path.exists(self.path):
                return self._resolved_future([])
            with open(self.path, "r", encoding="utf-8") as fh:
                text = fh.read()
            return self._resolved_future(parse_file(text))
        except Exception as exc:
            return self._resolved_future(exc=exc)

    def save(self, items: List[Item], *, make_backup: bool = True) -> Future:
        try:
            ssh_dir = os.path.dirname(self.path)
            os.makedirs(ssh_dir, exist_ok=True)
            try:
                os.chmod(ssh_dir, 0o700)
            except OSError as exc:
                logger.debug("chmod 700 on %s failed: %s", ssh_dir, exc)

            if make_backup and not self._backup_done and os.path.exists(self.path):
                backup = f"{self.path}.bak-{int(time.time())}"
                os.replace(self.path, backup)
                logger.info("Backed up %s -> %s", self.path, backup)

            tmp = self.path + ".sshpilot.tmp"
            payload = serialize(items).encode("utf-8")
            with open(tmp, "wb") as fh:
                fh.write(payload)
            try:
                os.chmod(tmp, 0o600)
            except OSError as exc:
                logger.debug("chmod on tmp failed: %s", exc)
            os.replace(tmp, self.path)
            os.chmod(self.path, 0o600)
            if make_backup and not self._backup_done:
                self._backup_done = True
            return self._resolved_future(None)
        except Exception as exc:
            return self._resolved_future(exc=exc)
