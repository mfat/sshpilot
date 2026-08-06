"""Tests for daemon-only public-key selection in the deployment UI."""

from types import SimpleNamespace
from unittest.mock import MagicMock

from sshpilot import sshcopyid_window as win_mod


def test_browsed_public_key_is_not_added_as_a_local_fallback():
    window = SimpleNamespace(
        _closed=False,
        _existing_keys_cache=[],
        _error=MagicMock(),
        _revert_dropdown_selection=MagicMock(),
    )

    win_mod.SshCopyIdWindow._add_browsed_public_key(
        window, "/home/alice/keys/server.pub"
    )

    assert window._existing_keys_cache == []
    window._error.assert_called_once()
    window._revert_dropdown_selection.assert_called_once()


def test_daemon_key_selection_uses_an_opaque_key_id():
    key = SimpleNamespace(key_id="key:daemon-1", name="server")
    assert key.key_id == "key:daemon-1"
    assert not hasattr(key, "private_path")
