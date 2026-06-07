"""Async service for loading and saving a remote ``~/.ssh/authorized_keys`` file.

Sits on top of :class:`AsyncSFTPManager` from ``file_manager_window`` and
returns futures so callers can dispatch back to the GTK main thread via the
existing pattern.
"""

from __future__ import annotations

import logging
import os
import posixpath
import shutil
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
            if sftp is None:
                raise RuntimeError("SFTP session is not connected")
            _, ssh_dir, ak_path = self._resolve_paths(sftp)

            # Ensure ~/.ssh exists with mode 0700. Use SFTP rather than
            # ssh.exec_command so we don't depend on a POSIX login shell
            # being available on the remote (Windows OpenSSH, restricted
            # shells, etc).
            try:
                sftp.mkdir(ssh_dir, 0o700)
            except IOError:
                # Already exists — that's fine.
                pass
            try:
                sftp.chmod(ssh_dir, 0o700)
            except IOError as exc:
                # Non-fatal: continue and let the write fail with a clearer
                # error if the directory really isn't writable.
                logger.debug("chmod 700 on %s failed: %s", ssh_dir, exc)

            # Back up the existing file by *copying* (not renaming). A
            # rename would leave no authorized_keys on the host between the
            # rename and the subsequent atomic install — if the connection
            # dies there, the user is locked out. A copy keeps the live
            # file in place until the final posix_rename swaps it.
            if do_backup:
                try:
                    sftp.stat(ak_path)
                except IOError:
                    pass  # nothing to back up
                else:
                    backup_path = f"{ak_path}.bak-{int(time.time())}"
                    with sftp.file(ak_path, "r") as src:
                        existing = src.read()
                    with sftp.file(backup_path, "w") as dst:
                        dst.write(existing)
                    try:
                        sftp.chmod(backup_path, 0o600)
                    except IOError as exc:
                        logger.debug("chmod 600 on backup failed: %s", exc)
                    logger.info("Backed up authorized_keys to %s", backup_path)

            # Create tmp file, set restrictive mode *before* writing the
            # content, so the keys are never world-readable on the remote.
            tmp_path = ak_path + ".sshpilot.tmp"
            payload = serialized.encode("utf-8")
            fh = sftp.file(tmp_path, "w")
            try:
                try:
                    sftp.chmod(tmp_path, 0o600)
                except IOError as exc:
                    logger.debug("chmod on empty tmp failed: %s", exc)
                fh.write(payload)
            finally:
                fh.close()
            # posix_rename preserves the source's mode, so the final file
            # is 0600 without a follow-up chmod.
            sftp.posix_rename(tmp_path, ak_path)

        future = self._manager._submit(_do)

        def _mark_backup_done(fut: Future) -> None:
            if fut.exception() is None and do_backup:
                self._backup_done = True

        future.add_done_callback(_mark_backup_done)
        return future


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

            # Backup is a copy, not a rename, so the live file stays in
            # place if anything goes wrong before the final replace.
            if make_backup and not self._backup_done and os.path.exists(self.path):
                backup = f"{self.path}.bak-{int(time.time())}"
                shutil.copy2(self.path, backup)
                try:
                    os.chmod(backup, 0o600)
                except OSError as exc:
                    logger.debug("chmod 600 on backup failed: %s", exc)
                logger.info("Backed up %s -> %s", self.path, backup)

            # Create the tmp file with restrictive perms *before* writing
            # any content, so the keys are never world-readable.
            tmp = self.path + ".sshpilot.tmp"
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                payload = serialize(items).encode("utf-8")
                os.write(fd, payload)
            finally:
                os.close(fd)
            os.replace(tmp, self.path)
            # Belt-and-braces: ensure final file is 0600 even if the
            # platform's umask interfered with O_CREAT mode.
            os.chmod(self.path, 0o600)
            if make_backup and not self._backup_done:
                self._backup_done = True
            return self._resolved_future(None)
        except Exception as exc:
            return self._resolved_future(exc=exc)
