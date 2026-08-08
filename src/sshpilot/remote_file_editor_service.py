"""Daemon-backed remote file service for the raw text editor.

The editor keeps all presentation; every file I/O (read, replace, and the
elevated sudo variants) is owned by the daemon through the typed SFTP file
RPCs. This adapter exposes the same ``daemon_file_service`` interface the
editor already consumes (``load`` / ``save_text`` returning futures) and adds
optional privileged methods (``load_privileged`` / ``save_text_privileged``)
that pass ``access=Sudo``. The sudo password is resolved daemon-side via the
interaction broker; the frontend never builds sudo commands or touches the
keyring here.
"""

from __future__ import annotations

import logging
from concurrent.futures import Future, ThreadPoolExecutor
from types import SimpleNamespace
from typing import Any

from .api.models.operations import (
    SftpFileAccess,
    SftpFileTarget,
    SftpReadFileRequest,
    SftpReplaceFileRequest,
)

logger = logging.getLogger(__name__)


class DaemonRemoteFileService:
    """GTK adapter over daemon-owned SFTP file read/replace.

    Mirrors the ``daemon_file_service`` interface consumed by
    ``RemoteFileEditorWindow`` so the editor reuses its existing UI unchanged.
    Privileged operations require the daemon ``sftp.privileged_file``
    capability; the daemon surfaces a stable structured error when the
    privileged runner is absent — there is no frontend sudo fallback.
    """

    #: This instance is dedicated to exactly one editor window, so the editor
    #: owns its lifecycle (same marker as the SSH-config editor service).
    editor_owned = True

    def __init__(
        self,
        client: Any,
        service_id: Any,
        path: str,
        *,
        privileged_supported: bool = False,
    ) -> None:
        if client is None or service_id is None:
            raise ValueError("a daemon client and an SFTP service id are required")
        self._client = client
        self._service_id = service_id
        self._path = path
        self._privileged_supported = bool(privileged_supported)
        self._revision: str = ""
        self._privileged_revision: str = ""
        self._executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="sshpilot-remote-file-editor",
        )

    # -- editor-facing interface -------------------------------------------

    def load(self) -> Future:
        """Read the remote file as the login user off the GTK thread."""

        def _do() -> SimpleNamespace:
            result = self._client.sftp_read_file(
                SftpReadFileRequest(
                    SftpFileTarget.REMOTE,
                    self._path,
                    self._service_id,
                    access=SftpFileAccess.NORMAL,
                )
            )
            self._revision = result.revision
            return SimpleNamespace(
                revision=result.revision,
                content=result.content,
                display_name=self._path,
                writable=True,
            )

        return self._executor.submit(_do)

    def save_text(self, text: str, *, make_backup: bool = True) -> Future:
        """Replace the remote file as the login user, revision-safe."""

        def _do() -> Any:
            result = self._client.sftp_replace_file(
                SftpReplaceFileRequest(
                    SftpFileTarget.REMOTE,
                    self._path,
                    text,
                    self._revision,
                    backup=make_backup,
                    service_id=self._service_id,
                    access=SftpFileAccess.NORMAL,
                )
            )
            self._revision = result.revision
            return result

        return self._executor.submit(_do)

    def load_privileged(self) -> Future:
        """Read the remote file as root through the daemon privileged runner."""

        def _do() -> SimpleNamespace:
            result = self._client.sftp_read_file(
                SftpReadFileRequest(
                    SftpFileTarget.REMOTE,
                    self._path,
                    self._service_id,
                    access=SftpFileAccess.SUDO,
                )
            )
            self._privileged_revision = result.revision
            return SimpleNamespace(
                revision=result.revision,
                content=result.content,
                exists=result.exists,
                display_name=self._path,
                writable=True,
            )

        return self._executor.submit(_do)

    def save_text_privileged(self, text: str, *, make_backup: bool = True) -> Future:
        """Replace the remote file as root, revision-safe."""

        def _do() -> Any:
            result = self._client.sftp_replace_file(
                SftpReplaceFileRequest(
                    SftpFileTarget.REMOTE,
                    self._path,
                    text,
                    self._privileged_revision or self._revision,
                    backup=make_backup,
                    service_id=self._service_id,
                    access=SftpFileAccess.SUDO,
                )
            )
            self._privileged_revision = result.revision
            return result

        return self._executor.submit(_do)

    # -- accessors ---------------------------------------------------------

    @property
    def privileged_supported(self) -> bool:
        return self._privileged_supported

    @property
    def display_name(self) -> str:
        return self._path

    @property
    def writable(self) -> bool:
        return True

    def close(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)
