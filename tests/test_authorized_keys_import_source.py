"""Authorized-keys window: importing keys an online identity publishes.

The fetch goes through ``client.fetch_public_keys`` off the GTK thread (never a
frontend HTTP request), keys the file already authorizes are skipped, errors
render from their structured detail code, and callbacks after close do nothing.
"""

from unittest.mock import MagicMock

import pytest

pytest.importorskip("gi")

from sshpilot import authorized_keys_window as win_mod
from sshpilot.api.errors import ErrorCode, SshPilotError
from sshpilot.api.models.identity import (
    PUBLIC_KEY_SOURCE_RATE_LIMITED,
    FetchPublicKeysRequest,
    ImportedPublicKey,
    ImportedPublicKeyList,
)
from sshpilot.authorized_keys_parser import AuthorizedKeyEntry, parse_file

ED25519 = "AAAAC3NzaC1lZDI1NTE5AAAAIMkGoTfVoNpsJrNxzq9WpRhlCp0qsPwsOHopWxNbIM8Z"
ED25519_FP = "SHA256:XCEFfvF2V6u0ESVb/GLp/HAHVCZMoi36uskzHxTBUk0"
ED25519_B = "AAAAC3NzaC1lZDI1NTE5AAAAIBZD0BJ4cbO7gdR0ncScv++/uuVhNyVZIchrfaM4qcVs"
ED25519_B_FP = "SHA256:KNENJxaBWf6prhHPSPcMharb4yJpo2MDW6zqiOt9xW4"


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
def _threads(monkeypatch):
    _ThreadSpy.instances = []
    monkeypatch.setattr(win_mod.threading, "Thread", _ThreadSpy)


@pytest.fixture
def idle(monkeypatch):
    callbacks = []
    monkeypatch.setattr(win_mod.GLib, "idle_add", lambda fn, *args: callbacks.append((fn, args)))
    return callbacks


def _make_window(items=None):
    window = win_mod.AuthorizedKeysWindow.__new__(win_mod.AuthorizedKeysWindow)
    window._closing = False
    window._listing_keys = False
    window._reading_public = False
    window._client = MagicMock()
    window._key_manager = MagicMock()
    window._items = list(items or [])
    window._toast = MagicMock()
    window._add_button = MagicMock()
    window._set_status = MagicMock()
    window._set_dirty = MagicMock()
    window._refresh_list = MagicMock()
    return window


def _result(*pairs, source="gh:mfat"):
    return ImportedPublicKeyList(
        source=source,
        keys=tuple(
            ImportedPublicKey(
                key_type="ssh-ed25519",
                fingerprint=fp,
                comment=f"# ssh-import-id {source}",
                line=f"ssh-ed25519 {blob} # ssh-import-id {source}",
            )
            for blob, fp in pairs
        ),
    )


def _run_fetch(window, idle):
    thread = _ThreadSpy.instances[-1]
    thread.target(*thread.args)
    fn, args = idle.pop(0)
    fn(*args)


def test_fetch_runs_through_daemon_client_off_the_gtk_thread(idle):
    window = _make_window()
    window._client.fetch_public_keys.return_value = _result((ED25519, ED25519_FP))

    window._start_public_key_fetch("gh:mfat")

    assert len(_ThreadSpy.instances) == 1
    thread = _ThreadSpy.instances[0]
    assert thread.target.__func__.__name__ == "_fetch_public_keys_worker"
    assert thread.daemon is True
    window._add_button.set_sensitive.assert_any_call(False)
    window._client.fetch_public_keys.assert_not_called()

    _run_fetch(window, idle)

    window._client.fetch_public_keys.assert_called_once_with(
        FetchPublicKeysRequest(source="gh:mfat")
    )
    entries = [it for it in window._items if isinstance(it, AuthorizedKeyEntry)]
    assert [(e.key_b64, e.comment, e.dirty) for e in entries] == [
        (ED25519, "# ssh-import-id gh:mfat", True)
    ]
    window._set_dirty.assert_called_once_with(True)
    window._add_button.set_sensitive.assert_any_call(True)
    assert window._fetching_keys is False
    assert window._toast.call_args[0][0] == "Added 1 key"


def test_keys_already_authorized_are_skipped(idle):
    existing = parse_file(f"ssh-ed25519 {ED25519} me@laptop\n")
    window = _make_window(existing)
    window._client.fetch_public_keys.return_value = _result(
        (ED25519, ED25519_FP), (ED25519_B, ED25519_B_FP)
    )

    window._start_public_key_fetch("gh:mfat")
    _run_fetch(window, idle)

    entries = [it for it in window._items if isinstance(it, AuthorizedKeyEntry)]
    assert [e.key_b64 for e in entries] == [ED25519, ED25519_B]
    assert window._toast.call_args[0][0] == "Added 1 key · 1 key was already authorized"


def test_all_keys_present_leaves_file_clean(idle):
    window = _make_window(parse_file(f"ssh-ed25519 {ED25519} me\n"))
    window._client.fetch_public_keys.return_value = _result((ED25519, ED25519_FP))

    window._start_public_key_fetch("gh:mfat")
    _run_fetch(window, idle)

    window._set_dirty.assert_not_called()
    assert window._toast.call_args[0][0] == "1 key was already authorized"


def test_structured_error_renders_its_message(idle):
    window = _make_window()
    window._client.fetch_public_keys.side_effect = SshPilotError(
        ErrorCode.KEY_PUBLIC_UNAVAILABLE,
        "raw daemon text",
        details={"code": PUBLIC_KEY_SOURCE_RATE_LIMITED},
    )

    window._start_public_key_fetch("gh:mfat")
    _run_fetch(window, idle)

    message = window._toast.call_args[0][0]
    assert "rate limit" in message
    assert "raw daemon text" not in message
    assert window._items == []
    assert window._fetching_keys is False


def test_concurrent_fetches_start_one_worker(idle):
    window = _make_window()
    window._start_public_key_fetch("gh:mfat")
    window._start_public_key_fetch("gh:mfat")
    window._on_add_from_local()

    assert len(_ThreadSpy.instances) == 1


def test_close_during_fetch_suppresses_append(idle):
    window = _make_window()
    window._client.fetch_public_keys.return_value = _result((ED25519, ED25519_FP))
    window._start_public_key_fetch("gh:mfat")
    window._add_button.set_sensitive.reset_mock()

    thread = _ThreadSpy.instances[0]
    thread.target(*thread.args)
    window._closing = True
    fn, args = idle[0]
    fn(*args)

    assert window._items == []
    window._toast.assert_not_called()
    window._add_button.set_sensitive.assert_not_called()
    assert window._fetching_keys is False


def test_missing_daemon_client_starts_no_work(idle):
    window = _make_window()
    window._client = None

    window._start_public_key_fetch("gh:mfat")

    assert _ThreadSpy.instances == []
    assert "background service" in window._toast.call_args[0][0]
