"""Daemon-backed SSH config text service for the raw text editor.

The GTK frontend never resolves which SSH config file is active and never
writes the file itself. The daemon selects the root (normal or isolated
mode), computes revisions, performs the hardened atomic write (revision
check, backup, permissions, symlink refusal), and reloads connection state.
This adapter only relays text and revisions over the daemon client, keeping
the exact editor UI from the removed local-file editor.
"""

from __future__ import annotations

import logging
from concurrent.futures import Future, ThreadPoolExecutor

from sshpilot.api.models.connections import SaveSshConfigTextRequest

logger = logging.getLogger(__name__)


class DaemonSshConfigTextService:
    """GTK adapter over daemon-owned SSH config text read/write.

    Mirrors the ``daemon_file_service`` interface consumed by
    ``RemoteFileEditorWindow`` (``load`` / ``save_text`` returning futures)
    so the SSH config editor reuses the existing editor UI unchanged.
    """

    #: This service instance is dedicated to exactly one editor window, so the
    #: editor owns its lifecycle. Shared services (e.g. the authorized-keys
    #: service) omit this marker and are never closed by an editor.
    editor_owned = True

    def __init__(self, client) -> None:
        if client is None:
            raise ValueError("a daemon client is required")
        self._client = client
        self._revision: str = ""
        self._display_name: str = ""
        self._writable: bool = True
        self._executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="sshpilot-ssh-config-editor",
        )

    # -- editor-facing interface -------------------------------------------

    def load(self) -> Future:
        """Load the daemon-selected SSH config text off the GTK thread."""

        def _do():
            result = self._client.get_ssh_config_text()
            self._revision = result.revision
            self._display_name = result.display_name
            self._writable = result.writable
            return result

        return self._executor.submit(_do)

    def save_text(self, text: str, *, make_backup: bool = True) -> Future:
        """Save the edited text through the daemon's hardened write path.

        ``make_backup`` is accepted for interface parity; the daemon always
        performs the one-shot ``.bak`` backup and validates/reloads the edited
        document before publishing it.
        """
        del make_backup

        def _do():
            result = self._client.save_ssh_config_text(
                SaveSshConfigTextRequest(
                    text=text,
                    expected_revision=self._revision,
                )
            )
            self._revision = result.revision
            self._display_name = result.display_name
            self._writable = result.writable
            return result

        return self._executor.submit(_do)

    # -- accessors ---------------------------------------------------------

    @property
    def display_name(self) -> str:
        return self._display_name

    @property
    def writable(self) -> bool:
        return self._writable

    def close(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)

    @staticmethod
    def _failed_future(exc: BaseException) -> Future:
        future: Future = Future()
        future.set_exception(exc)
        return future
