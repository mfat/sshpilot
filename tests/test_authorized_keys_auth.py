"""Authorized-key editor authentication and connection ownership boundary.

The editor no longer owns passwords, SFTP managers, or connection state: it
loads and replaces ``authorized_keys`` through typed daemon file calls
(``sftp.read_file`` / ``sftp.replace_file``). Any password, host-key, or
askpass prompt is handled by the daemon's protected-interaction broker, so the
window must not reimplement connect/password helpers or fall back to a local
SFTP backend.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

pytest.importorskip("gi")

from sshpilot import authorized_keys_window as win_mod
from sshpilot.api.models.operations import (
    SftpFileTarget,
    SftpReadFileRequest,
    SftpReplaceFileRequest,
)


def test_window_has_no_local_password_or_connect_helpers():
    """GTK no longer owns authentication for the authorized-key editor."""
    window = win_mod.AuthorizedKeysWindow.__new__(win_mod.AuthorizedKeysWindow)
    for helper in (
        "_manager_has_password",
        "_ensure_password_before_connect",
        "_handle_auth_required",
        "_prompt_for_password",
        "_materialize_agent_pubkey",
    ):
        assert not hasattr(window, helper), (
            f"AuthorizedKeysWindow still owns {helper!r} (daemon-ownership debt)"
        )


def test_window_has_no_sftp_manager_fallback():
    """The window's service is daemon-backed; it never constructs an SFTP manager."""
    window = win_mod.AuthorizedKeysWindow.__new__(win_mod.AuthorizedKeysWindow)
    assert not hasattr(window, "_manager")
    assert not hasattr(window, "_connection_manager")
    assert not hasattr(window, "_password")


def test_service_requires_a_daemon_client():
    with pytest.raises(ValueError):
        win_mod.DaemonAuthorizedKeysService(None)


def test_local_service_forbids_a_service_id():
    client = MagicMock()
    with pytest.raises(ValueError):
        win_mod.DaemonAuthorizedKeysService(client, service_id="sftp:1", local=True)


def test_remote_service_requires_a_service_id():
    client = MagicMock()
    with pytest.raises(ValueError):
        win_mod.DaemonAuthorizedKeysService(client, local=False)


def test_local_load_uses_typed_local_target():
    client = MagicMock()
    client.sftp_read_file.return_value = SimpleNamespace(
        target=SftpFileTarget.LOCAL_AUTHORIZED_KEYS,
        path="/home/u/.ssh/authorized_keys",
        content="ssh-ed25519 AAAA comment\n",
        exists=True,
        revision="akf-1",
        size=30,
    )
    service = win_mod.DaemonAuthorizedKeysService(client, local=True)

    future = service.load()
    result = future.result()

    client.sftp_read_file.assert_called_once()
    request = client.sftp_read_file.call_args[0][0]
    assert isinstance(request, SftpReadFileRequest)
    assert request.target is SftpFileTarget.LOCAL_AUTHORIZED_KEYS
    assert request.path == "~/.ssh/authorized_keys"
    assert result[0].comment == "comment"


def test_remote_save_uses_typed_revision_check():
    client = MagicMock()
    client.sftp_replace_file.return_value = SimpleNamespace(revision="akf-2")
    service = win_mod.DaemonAuthorizedKeysService(
        client, service_id="sftp:1", local=False
    )
    service._revision = "akf-1"

    service.save_text("ssh-ed25519 AAAA comment\n", make_backup=True)

    client.sftp_replace_file.assert_called_once()
    request = client.sftp_replace_file.call_args[0][0]
    assert isinstance(request, SftpReplaceFileRequest)
    assert request.target is SftpFileTarget.REMOTE
    assert request.service_id == "sftp:1"
    assert request.expected_revision == "akf-1"
    assert request.backup is True
