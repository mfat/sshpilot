"""ssh-copy-id existing-key loading runs off the GTK thread.

Loading guards: a single background listing, controls disabled while running,
callbacks ignored after close, safe errors without paths, and dropdown labels
populated from the daemon ``SSHKey.name`` (never basename-of-path for
daemon-discovered keys).
"""

from unittest.mock import MagicMock

import pytest

pytest.importorskip("gi")

from sshpilot import sshcopyid_window as win_mod
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
    window = win_mod.SshCopyIdWindow.__new__(win_mod.SshCopyIdWindow)
    window._closed = False
    window._loading_keys = False
    window._generating = False
    window._km = MagicMock()
    window._parent = MagicMock()
    window._conn = object()
    window._cm = None
    window._existing_keys_cache = []
    window._last_real_selection = 0
    window.dropdown_existing = MagicMock()
    window.radio_existing = MagicMock()
    window.radio_existing.get_active.return_value = True
    window.radio_generate = MagicMock()
    window.radio_generate.get_active.return_value = False
    window.btn_ok = MagicMock()
    window.row_key_name = MagicMock()
    window.type_dropdown = MagicMock()
    window.row_pass_toggle = MagicMock()
    window.pass1 = MagicMock()
    window.pass2 = MagicMock()
    window.pass_box = MagicMock()
    window.force_toggle = MagicMock()
    window._server_label = MagicMock()
    window._server_row = MagicMock()
    window._error = MagicMock()
    window._info = MagicMock()
    window.close = MagicMock()
    window._rebuild_existing_dropdown = MagicMock()
    return window


def test_key_loading_runs_in_a_daemon_thread(monkeypatch, idle):
    monkeypatch.setattr(win_mod.threading, "Thread", _ThreadSpy)
    window = _make_window()
    window.radio_existing.get_active.return_value = True

    window._reload_existing_keys()

    assert len(_ThreadSpy.instances) == 1
    thread = _ThreadSpy.instances[0]
    assert thread.target.__self__ is window
    assert thread.target.__func__.__name__ == "_load_keys_worker"
    assert thread.daemon is True
    # Existing-key controls disabled while loading; OK off in existing mode.
    window.dropdown_existing.set_sensitive.assert_any_call(False)
    window.btn_ok.set_sensitive.assert_any_call(False)


def test_repeated_load_is_rejected(monkeypatch, idle):
    monkeypatch.setattr(win_mod.threading, "Thread", _ThreadSpy)
    window = _make_window()

    window._reload_existing_keys()
    window._reload_existing_keys()

    assert len(_ThreadSpy.instances) == 1


def test_key_load_success_populates_names_from_dto(monkeypatch, idle):
    monkeypatch.setattr(win_mod.threading, "Thread", _ThreadSpy)
    window = _make_window()
    window._reload_existing_keys()
    thread = _ThreadSpy.instances[0]
    window._km.discover_keys.return_value = [
        SSHKey("/daemon/keys/id_ed25519", name="id_ed25519"),
    ]

    thread.target(*thread.args)
    fn, args = idle[0]
    fn(*args)

    names = window._rebuild_existing_dropdown.call_args[0][0]
    assert names == ["id_ed25519"]
    # Controls re-enabled on completion.
    window.dropdown_existing.set_sensitive.assert_any_call(True)
    window.btn_ok.set_sensitive.assert_any_call(True)
    assert window._existing_keys_cache[0].name == "id_ed25519"


def test_key_load_success_with_no_keys_shows_placeholder(monkeypatch, idle):
    monkeypatch.setattr(win_mod.threading, "Thread", _ThreadSpy)
    window = _make_window()
    window._reload_existing_keys()
    thread = _ThreadSpy.instances[0]
    window._km.discover_keys.return_value = []

    thread.target(*thread.args)
    fn, args = idle[0]
    fn(*args)

    names = window._rebuild_existing_dropdown.call_args[0][0]
    assert "No keys found" in names[0]


def test_key_load_failure_shows_safe_error_without_paths(monkeypatch, idle):
    monkeypatch.setattr(win_mod.threading, "Thread", _ThreadSpy)
    window = _make_window()
    window._reload_existing_keys()
    thread = _ThreadSpy.instances[0]
    window._km.discover_keys.side_effect = RuntimeError(
        "boom /home/user/.ssh/private_key"
    )

    thread.target(*thread.args)
    fn, args = idle[0]
    fn(*args)

    window._error.assert_called_once()
    rendered = " ".join(str(part) for part in window._error.call_args[0])
    assert "/home/user/.ssh" not in rendered
    assert "private_key" not in rendered


def test_close_during_load_suppresses_widget_updates(monkeypatch, idle):
    monkeypatch.setattr(win_mod.threading, "Thread", _ThreadSpy)
    window = _make_window()
    window._reload_existing_keys()
    thread = _ThreadSpy.instances[0]
    window._km.discover_keys.return_value = [SSHKey("/daemon/keys/k", name="k")]

    thread.target(*thread.args)
    window._closed = True
    fn, args = idle[0]
    fn(*args)

    window._rebuild_existing_dropdown.assert_not_called()
    window._error.assert_not_called()


def test_close_prevents_starting_a_new_load(monkeypatch, idle):
    monkeypatch.setattr(win_mod.threading, "Thread", _ThreadSpy)
    window = _make_window()
    window._closed = True

    window._reload_existing_keys()

    assert _ThreadSpy.instances == []


def test_browse_does_not_create_a_local_key_fallback():
    window = _make_window()
    window._existing_keys_cache = []

    window._add_browsed_public_key("/home/alice/keys/server.pub")

    assert window._existing_keys_cache == []
    window._error.assert_called_once()
    window._rebuild_existing_dropdown.assert_not_called()


# ---------------------------------------------------------------------------
# Centralized OK sensitivity
# ---------------------------------------------------------------------------
def test_initial_loading_keeps_ok_insensitive_after_set_server():
    """The constructor ordering bug: _set_server must not re-enable OK while
    the initial key listing is still in flight."""
    window = _make_window()
    window._loading_keys = True
    window._existing_keys_cache = [SSHKey("/daemon/keys/k", name="k")]
    window.btn_ok.set_sensitive.reset_mock()

    window._set_server(object())

    window.btn_ok.set_sensitive.assert_called_with(False)


def test_existing_key_ok_disabled_when_list_empty_or_failed():
    window = _make_window()
    window._conn = object()
    window._existing_keys_cache = []

    window._update_ok_sensitivity()
    window.btn_ok.set_sensitive.assert_called_with(False)

    # A failed load leaves the cache empty, so OK stays off.
    window.btn_ok.set_sensitive.reset_mock()
    window._on_keys_load_failed(RuntimeError("boom"))
    window.btn_ok.set_sensitive.assert_called_with(False)


def test_browsing_a_key_keeps_ok_disabled():
    window = _make_window()
    window._conn = object()
    window._existing_keys_cache = []
    window.btn_ok.set_sensitive.reset_mock()

    window._add_browsed_public_key("/home/alice/keys/server.pub")

    window.btn_ok.set_sensitive.assert_not_called()


def test_ok_click_does_nothing_while_closed_loading_or_generating(monkeypatch):
    monkeypatch.setattr(win_mod.threading, "Thread", _ThreadSpy)
    window = _make_window()

    window._closed = True
    window._on_ok_clicked()
    assert _ThreadSpy.instances == []

    window._closed = False
    window._loading_keys = True
    window._on_ok_clicked()
    assert _ThreadSpy.instances == []

    window._loading_keys = False
    window._generating = True
    window._on_ok_clicked()
    assert _ThreadSpy.instances == []


def test_load_completion_after_close_does_not_touch_widgets(monkeypatch, idle):
    monkeypatch.setattr(win_mod.threading, "Thread", _ThreadSpy)
    window = _make_window()
    window._reload_existing_keys()
    thread = _ThreadSpy.instances[0]
    window._km.discover_keys.return_value = [SSHKey("/daemon/keys/k", name="k")]
    window.btn_ok.set_sensitive.reset_mock()
    window.dropdown_existing.set_sensitive.reset_mock()

    thread.target(*thread.args)
    window._closed = True
    fn, args = idle[0]
    fn(*args)

    window.btn_ok.set_sensitive.assert_not_called()
    window.dropdown_existing.set_sensitive.assert_not_called()
    assert window._loading_keys is False
