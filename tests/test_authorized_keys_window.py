"""Authorized-keys window: daemon-discovered public keys are read via the API.

The selected key's public text comes from ``key_manager.read_public_key()``
off the GTK thread — never from a direct ``.pub`` file read. Multiple clicks
start one read, callbacks are ignored after close, and unavailable public keys
produce a clear path-free message.
"""

from concurrent.futures import Future
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

pytest.importorskip("gi")

from sshpilot import authorized_keys_window as win_mod
from sshpilot.api.errors import ErrorCode, SshPilotError
from sshpilot.key_manager import SSHKey


class _ThreadSpy:
    instances: list = []

    def __init__(self, target=None, args=(), daemon=False):
        self.target = target
        self.args = args
        self.daemon = daemon
        _ThreadSpy.instances.append(self)

    def start(self):
        pass


@pytest.fixture(autouse=True)
def _reset_spy():
    _ThreadSpy.instances = []
    yield


@pytest.fixture
def idle(monkeypatch):
    callbacks = []
    monkeypatch.setattr(win_mod.GLib, "idle_add", lambda fn, *args: callbacks.append((fn, args)))
    return callbacks


def _make_window():
    window = win_mod.AuthorizedKeysWindow.__new__(win_mod.AuthorizedKeysWindow)
    window._closing = False
    window._listing_keys = False
    window._reading_public = False
    window._key_manager = MagicMock()
    window._connection_manager = None
    window._toast = MagicMock()
    window._add_button = MagicMock()
    window._append_pubkey_text = MagicMock()
    window._prompt_local_key_pick = MagicMock()
    return window


def _key(name="id_ed25519", key_id="key-1"):
    return SSHKey(f"/daemon/keys/{name}", name=name, key_id=key_id)


# ---------------------------------------------------------------------------
# Public-key read flow
# ---------------------------------------------------------------------------
def test_selected_key_reads_public_text_through_daemon(monkeypatch, idle):
    monkeypatch.setattr(win_mod.threading, "Thread", _ThreadSpy)
    window = _make_window()
    key = _key()

    window._start_public_key_read(key)

    assert len(_ThreadSpy.instances) == 1
    thread = _ThreadSpy.instances[0]
    assert thread.target.__func__.__name__ == "_read_public_worker"
    assert thread.args[1] is key
    assert thread.daemon is True
    window._add_button.set_sensitive.assert_any_call(False)

    window._key_manager.read_public_key.return_value = "ssh-ed25519 AAAA-public\n"
    thread.target(*thread.args)
    fn, args = idle[0]
    fn(*args)

    window._append_pubkey_text.assert_called_once_with("ssh-ed25519 AAAA-public\n")
    window._add_button.set_sensitive.assert_any_call(True)


def test_multiple_read_clicks_start_one_read(monkeypatch, idle):
    monkeypatch.setattr(win_mod.threading, "Thread", _ThreadSpy)
    window = _make_window()
    key = _key()
    window._reading_public = True

    window._start_public_key_read(key)

    assert _ThreadSpy.instances == []


def test_public_unavailable_shows_clear_message(monkeypatch, idle):
    monkeypatch.setattr(win_mod.threading, "Thread", _ThreadSpy)
    window = _make_window()
    window._start_public_key_read(_key())
    thread = _ThreadSpy.instances[0]
    window._key_manager.read_public_key.side_effect = SshPilotError(
        ErrorCode.KEY_PUBLIC_UNAVAILABLE, "public key is missing"
    )

    thread.target(*thread.args)
    fn, args = idle[0]
    fn(*args)

    message = str(window._toast.call_args[0][0])
    assert "not available" in message
    window._append_pubkey_text.assert_not_called()
    # No path leaks into the message.
    assert "/" not in message


def test_read_failure_restores_controls_and_shows_generic_error(monkeypatch, idle):
    monkeypatch.setattr(win_mod.threading, "Thread", _ThreadSpy)
    window = _make_window()
    window._start_public_key_read(_key())
    thread = _ThreadSpy.instances[0]
    window._key_manager.read_public_key.side_effect = RuntimeError(
        "boom /secret/keys"
    )

    thread.target(*thread.args)
    fn, args = idle[0]
    fn(*args)

    message = str(window._toast.call_args[0][0])
    assert "/secret/keys" not in message
    window._add_button.set_sensitive.assert_any_call(True)
    window._append_pubkey_text.assert_not_called()


def test_close_during_read_suppresses_append(monkeypatch, idle):
    monkeypatch.setattr(win_mod.threading, "Thread", _ThreadSpy)
    window = _make_window()
    window._start_public_key_read(_key())
    thread = _ThreadSpy.instances[0]
    window._key_manager.read_public_key.return_value = "ssh-ed25519 AAAA\n"

    thread.target(*thread.args)
    window._closing = True
    fn, args = idle[0]
    fn(*args)

    window._append_pubkey_text.assert_not_called()
    window._toast.assert_not_called()


def test_authorized_key_counts_use_real_plurals(monkeypatch):
    calls = []

    def translate(singular, plural, count):
        calls.append((singular, plural, count))
        return "{count} singular" if count == 1 else "{count} plural"

    monkeypatch.setattr(win_mod, "ngettext", translate)

    assert win_mod._loaded_entries_text(1) == "1 singular"
    assert win_mod._loaded_entries_text(2) == "2 plural"
    assert win_mod._added_keys_text(1) == "1 singular"
    assert win_mod._added_keys_text(3) == "3 plural"
    assert calls == [
        ("Loaded {count} entry", "Loaded {count} entries", 1),
        ("Loaded {count} entry", "Loaded {count} entries", 2),
        ("Added {count} key", "Added {count} keys", 1),
        ("Added {count} key", "Added {count} keys", 3),
    ]


def test_remote_load_uses_direct_sftp_error_formatter(monkeypatch):
    error = SshPilotError(ErrorCode.REMOTE_PATH_NOT_FOUND, "raw daemon message")
    future = Future()
    future.set_exception(error)
    window = win_mod.AuthorizedKeysWindow.__new__(win_mod.AuthorizedKeysWindow)
    window._local = True
    window._ready = True
    window._service = SimpleNamespace(load=lambda: future)
    window._set_status = MagicMock()
    window._toast = MagicMock()
    monkeypatch.setattr(win_mod.GLib, "idle_add", lambda fn, *args: fn(*args))
    formatter = MagicMock(return_value="localized SFTP error")
    monkeypatch.setattr(win_mod, "format_direct_sftp_error", formatter)

    window._reload()

    formatter.assert_called_once_with(error)
    window._toast.assert_called_once_with("Failed to load: localized SFTP error")


def test_remote_save_uses_direct_sftp_error_formatter(monkeypatch):
    error = SshPilotError(ErrorCode.REMOTE_PERMISSION_DENIED, "raw daemon message")
    future = Future()
    future.set_exception(error)
    window = win_mod.AuthorizedKeysWindow.__new__(win_mod.AuthorizedKeysWindow)
    window._dirty = True
    window._items = []
    window._service = SimpleNamespace(save=lambda *_args, **_kwargs: future)
    window._set_status = MagicMock()
    window._toast = MagicMock()
    window._save_button = MagicMock()
    monkeypatch.setattr(win_mod.GLib, "idle_add", lambda fn, *args: fn(*args))
    formatter = MagicMock(return_value="localized SFTP error")
    monkeypatch.setattr(win_mod, "format_direct_sftp_error", formatter)

    window._on_save_clicked(None)

    formatter.assert_called_once_with(error)
    window._toast.assert_called_once_with("Save failed: localized SFTP error")
