"""Password-connect helpers for the authorized_keys editor."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from sshpilot.authorized_keys_window import AuthorizedKeysWindow


def _bare_window(**overrides):
    """Construct an AuthorizedKeysWindow shell without running GTK init."""
    win = AuthorizedKeysWindow.__new__(AuthorizedKeysWindow)
    win._connection = overrides.get("connection", MagicMock(auth_method=1))
    win._manager = overrides.get("manager", MagicMock(_password=None))
    win._connection_manager = overrides.get("connection_manager")
    win._password_dialog_shown = False
    win._password_retry_count = 0
    win._max_password_retries = 3
    win._closing = False
    return win


def test_manager_has_password():
    win = _bare_window(manager=MagicMock(_password="secret"))
    assert win._manager_has_password() is True
    win = _bare_window(manager=MagicMock(_password="  "))
    assert win._manager_has_password() is False


def test_ensure_password_before_connect_skips_when_password_present():
    win = _bare_window(manager=MagicMock(_password="cached"))
    with patch.object(win, "_prompt_for_password") as prompt:
        assert win._ensure_password_before_connect() is True
    prompt.assert_not_called()


def test_ensure_password_before_connect_prompts_and_applies_password():
    win = _bare_window()
    with patch(
        "sshpilot.authorized_keys_window._is_password_auth_enabled",
        return_value=True,
    ):
        with patch.object(win, "_prompt_for_password", return_value="secret"):
            assert win._ensure_password_before_connect() is True
    assert win._manager._password == "secret"
    assert win._connection.password == "secret"


def test_ensure_password_before_connect_cancelled():
    win = _bare_window()
    with patch(
        "sshpilot.authorized_keys_window._is_password_auth_enabled",
        return_value=True,
    ):
        with patch.object(win, "_prompt_for_password", return_value=None):
            with patch.object(win, "_set_status"):
                assert win._ensure_password_before_connect() is False


def test_handle_auth_required_retries_with_password():
    win = _bare_window()
    manager = win._manager
    with patch(
        "sshpilot.authorized_keys_window._is_password_auth_enabled",
        return_value=True,
    ):
        with patch.object(win, "_prompt_for_password", return_value="retry-me"):
            win._handle_auth_required("Permission denied")
    assert manager._password == "retry-me"
    manager.connect_to_server.assert_called_once()
