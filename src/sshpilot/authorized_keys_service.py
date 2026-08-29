"""Async service for loading and saving a remote ``~/.ssh/authorized_keys`` file.

Uses the daemon-owned SFTP service and returns futures so callers can dispatch
back to the GTK main thread via the existing pattern.
"""

from __future__ import annotations

import logging
import os
import posixpath
import shutil
import time
from concurrent.futures import Future, ThreadPoolExecutor
from typing import List, Optional

from sshpilot.api.errors import ErrorCode, SshPilotError
from sshpilot.api.models.operations import (
    SftpFileTarget,
    SftpReadFileRequest,
    SftpReplaceFileRequest,
    SftpServiceState,
    SftpServiceSummary,
)

from .authorized_keys_parser import Item, parse_file, serialize
from .gtk.sftp_failure_messages import format_sftp_failure

logger = logging.getLogger(__name__)


REMOTE_BASENAME = "authorized_keys"
SSH_DIR_BASENAME = ".ssh"

# ``open_sftp`` returns a STARTING summary before the daemon has launched the
# OpenSSH SFTP child; this bounds how long we wait for READY before loading.
DEFAULT_SFTP_READY_TIMEOUT_SECONDS = 45.0


class DaemonAuthorizedKeysService:
    """GTK adapter over daemon-owned authorized_keys file operations."""

    def __init__(self, client, *, service_id=None, local: bool = False) -> None:
        if client is None:
            raise ValueError("a daemon client is required")
        if local and service_id is not None:
            raise ValueError("local authorized_keys has no SFTP service")
        if not local and service_id is None:
            raise ValueError("remote authorized_keys requires an SFTP service")
        self._client = client
        self._service_id = service_id
        self._local = local
        self._revision = "absent"
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="sshpilot-authorized-keys")

    def _request(self):
        return SftpReadFileRequest(
            target=(SftpFileTarget.LOCAL_AUTHORIZED_KEYS if self._local else SftpFileTarget.REMOTE),
            path=("~/.ssh/authorized_keys" if self._local else ".ssh/authorized_keys"),
            service_id=None if self._local else self._service_id,
        )

    def load(self) -> Future:
        def _do():
            result = self._client.sftp_read_file(self._request())
            self._revision = result.revision
            return parse_file(result.content) if result.exists else []

        return self._executor.submit(_do)

    def await_ready(
        self,
        *,
        timeout: float = DEFAULT_SFTP_READY_TIMEOUT_SECONDS,
    ) -> Future:
        """Wait until the daemon SFTP service is READY before file operations.

        ``open_sftp`` returns a STARTING summary before the OpenSSH SFTP child
        has connected. The daemon serializes every later file command behind
        that startup on the same service lane, so issuing ``sftp_read_file``
        immediately can exceed the client request timeout. Poll the live
        service summary until READY or a terminal state.
        """

        if self._local:
            raise ValueError(
                "local authorized_keys has no daemon SFTP service to wait for"
            )

        def _do() -> SftpServiceSummary:
            deadline = time.monotonic() + max(0.0, float(timeout))
            last_error: Optional[BaseException] = None
            while True:
                try:
                    summary = self._client.get_sftp_service(self._service_id)
                except Exception as exc:
                    last_error = exc
                else:
                    if summary.state is SftpServiceState.READY:
                        return summary
                    if summary.state in (
                        SftpServiceState.FAILED,
                        SftpServiceState.CLOSED,
                    ):
                        failure = getattr(summary, "failure", None)
                        detail = (
                            format_sftp_failure(failure)
                            if failure is not None
                            else "The SFTP session could not be established"
                        )
                        raise SshPilotError(
                            ErrorCode.SFTP_SERVICE_NOT_READY,
                            detail,
                        )
                if time.monotonic() >= deadline:
                    break
                time.sleep(0.1)
            raise last_error or SshPilotError(
                ErrorCode.SFTP_SERVICE_NOT_READY,
                "The SFTP service did not become ready in time",
            )

        return self._executor.submit(_do)

    def save(self, items: List[Item], *, make_backup: bool = True) -> Future:
        return self.save_text(serialize(items), make_backup=make_backup)

    def save_text(self, content: str, *, make_backup: bool = True) -> Future:
        request = SftpReplaceFileRequest(
            target=(SftpFileTarget.LOCAL_AUTHORIZED_KEYS if self._local else SftpFileTarget.REMOTE),
            path=("~/.ssh/authorized_keys" if self._local else ".ssh/authorized_keys"),
            content=content,
            expected_revision=self._revision,
            backup=make_backup,
            service_id=None if self._local else self._service_id,
        )

        def _do():
            result = self._client.sftp_replace_file(request)
            self._revision = result.revision
            return result

        return self._executor.submit(_do)

    def close(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)


class AuthorizedKeysService:
    """Load / save the remote authorized_keys file via the SFTP file manager."""

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

    def _install_atomic(self, sftp, tmp_path: str, ak_path: str) -> None:
        """Replace ``ak_path`` with ``tmp_path``, preferring OpenSSH posix-rename."""
        try:
            sftp.posix_rename(tmp_path, ak_path)
            return
        except OSError as exc:
            logger.debug(
                "posix-rename unsupported or failed (%s); falling back to rename",
                exc,
            )
        try:
            sftp.remove(ak_path)
        except OSError:
            pass
        sftp.rename(tmp_path, ak_path)
        try:
            sftp.chmod(ak_path, 0o600)
        except OSError as chmod_exc:
            logger.debug("chmod after rename fallback failed: %s", chmod_exc)

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
            except OSError:
                logger.debug("Remote ~/.ssh does not exist yet")
                return []
            try:
                with sftp.file(ak_path, "r") as fh:
                    data = fh.read()
            except OSError:
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
            except OSError:
                # Already exists — that's fine.
                pass
            try:
                sftp.chmod(ssh_dir, 0o700)
            except OSError as exc:
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
                except OSError:
                    pass  # nothing to back up
                else:
                    backup_path = f"{ak_path}.bak-{int(time.time())}"
                    with sftp.file(ak_path, "r") as src:
                        existing = src.read()
                    with sftp.file(backup_path, "w") as dst:
                        dst.write(existing)
                    try:
                        sftp.chmod(backup_path, 0o600)
                    except OSError as exc:
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
                except OSError as exc:
                    logger.debug("chmod on empty tmp failed: %s", exc)
                fh.write(payload)
            finally:
                fh.close()
            self._install_atomic(sftp, tmp_path, ak_path)

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
            with open(self.path, encoding="utf-8") as fh:
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
