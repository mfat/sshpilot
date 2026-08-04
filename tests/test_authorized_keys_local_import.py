"""Authorized-keys local key import runs through the daemon.

The window requires an injected daemon-backed key manager: there is no local
fallback construction and no directory scanning. Listing runs off the GTK
thread, repeated requests are rejected, callbacks are ignored after close, and
errors never expose paths.
"""

import ast
from pathlib import Path
from unittest.mock import MagicMock

import pytest

pytest.importorskip("gi")

from sshpilot import authorized_keys_window as win_mod
from sshpilot.key_manager import SSHKey

_SOURCE = (
    Path(__file__).resolve().parents[1]
    / "src" / "sshpilot" / "authorized_keys_window.py"
)


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
    window._key_manager = None
    window._connection_manager = None
    window._toast = MagicMock()
    window._add_button = MagicMock()
    window._append_pubkey_text = MagicMock()
    window._prompt_local_key_pick = MagicMock()
    return window


def _key(name="id_ed25519", key_id="key-1"):
    return SSHKey(f"/daemon/keys/{name}", name=name, key_id=key_id)


# ---------------------------------------------------------------------------
# Manager ownership
# ---------------------------------------------------------------------------
def test_missing_manager_shows_daemon_message_without_fallback(monkeypatch, idle):
    monkeypatch.setattr(win_mod.threading, "Thread", _ThreadSpy)
    window = _make_window()
    window._key_manager = None

    window._on_add_from_local()

    assert _ThreadSpy.instances == []
    assert window._key_manager is None
    message = str(window._toast.call_args[0][0])
    assert "background service" in message


def test_list_uses_injected_daemon_manager(monkeypatch, idle):
    monkeypatch.setattr(win_mod.threading, "Thread", _ThreadSpy)
    window = _make_window()
    window._key_manager = MagicMock()

    window._on_add_from_local()

    assert len(_ThreadSpy.instances) == 1
    thread = _ThreadSpy.instances[0]
    assert thread.target.__func__.__name__ == "_list_keys_worker"
    assert thread.args[0] is window._key_manager
    assert thread.daemon is True
    # Busy state: the add button is disabled while listing.
    window._add_button.set_sensitive.assert_any_call(False)


def test_repeated_listing_is_rejected(monkeypatch, idle):
    monkeypatch.setattr(win_mod.threading, "Thread", _ThreadSpy)
    window = _make_window()
    window._key_manager = MagicMock()

    window._on_add_from_local()
    window._on_add_from_local()

    assert len(_ThreadSpy.instances) == 1


# ---------------------------------------------------------------------------
# Listing completion
# ---------------------------------------------------------------------------
def test_list_success_prompts_with_dto_names(monkeypatch, idle):
    monkeypatch.setattr(win_mod.threading, "Thread", _ThreadSpy)
    window = _make_window()
    window._key_manager = MagicMock()
    window._on_add_from_local()
    thread = _ThreadSpy.instances[0]
    window._key_manager.discover_keys.return_value = [_key("k1"), _key("k2")]

    thread.target(*thread.args)
    fn, args = idle[0]
    fn(*args)

    window._prompt_local_key_pick.assert_called_once()
    picked = window._prompt_local_key_pick.call_args[0][0]
    assert [k.name for k in picked] == ["k1", "k2"]
    window._add_button.set_sensitive.assert_any_call(True)


def test_list_no_keys_shows_safe_message(monkeypatch, idle):
    monkeypatch.setattr(win_mod.threading, "Thread", _ThreadSpy)
    window = _make_window()
    window._key_manager = MagicMock()
    window._on_add_from_local()
    thread = _ThreadSpy.instances[0]
    window._key_manager.discover_keys.return_value = []

    thread.target(*thread.args)
    fn, args = idle[0]
    fn(*args)

    message = str(window._toast.call_args[0][0])
    assert "No local SSH keys found" in message
    window._prompt_local_key_pick.assert_not_called()


def test_list_failure_shows_error_without_paths(monkeypatch, idle):
    monkeypatch.setattr(win_mod.threading, "Thread", _ThreadSpy)
    window = _make_window()
    window._key_manager = MagicMock()
    window._on_add_from_local()
    thread = _ThreadSpy.instances[0]
    window._key_manager.discover_keys.side_effect = RuntimeError(
        "boom /home/user/.ssh/keys"
    )

    thread.target(*thread.args)
    fn, args = idle[0]
    fn(*args)

    message = str(window._toast.call_args[0][0])
    assert "/home/user/.ssh" not in message
    window._prompt_local_key_pick.assert_not_called()


def test_close_during_list_suppresses_prompt(monkeypatch, idle):
    monkeypatch.setattr(win_mod.threading, "Thread", _ThreadSpy)
    window = _make_window()
    window._key_manager = MagicMock()
    window._on_add_from_local()
    thread = _ThreadSpy.instances[0]
    window._key_manager.discover_keys.return_value = [_key()]

    thread.target(*thread.args)
    window._closing = True
    fn, args = idle[0]
    fn(*args)

    window._prompt_local_key_pick.assert_not_called()
    window._toast.assert_not_called()


# ---------------------------------------------------------------------------
# Teardown guards and flag hygiene
# ---------------------------------------------------------------------------
def test_start_public_key_read_after_teardown_does_nothing(monkeypatch, idle):
    monkeypatch.setattr(win_mod.threading, "Thread", _ThreadSpy)
    window = _make_window()
    window._key_manager = MagicMock()
    window._closing = True

    window._start_public_key_read(_key())

    assert _ThreadSpy.instances == []


def test_start_public_key_read_rejected_while_listing(monkeypatch, idle):
    monkeypatch.setattr(win_mod.threading, "Thread", _ThreadSpy)
    window = _make_window()
    window._key_manager = MagicMock()
    window._listing_keys = True

    window._start_public_key_read(_key())

    assert _ThreadSpy.instances == []


def test_add_from_local_after_teardown_starts_no_work(monkeypatch, idle):
    monkeypatch.setattr(win_mod.threading, "Thread", _ThreadSpy)
    window = _make_window()
    window._key_manager = MagicMock()
    window._closing = True

    window._on_add_from_local()

    assert _ThreadSpy.instances == []
    window._toast.assert_not_called()


def test_delayed_picker_response_after_teardown_starts_no_read(monkeypatch, idle):
    monkeypatch.setattr(win_mod.threading, "Thread", _ThreadSpy)
    captured = {}

    class _FakeDialog:
        def __init__(self, *args, **kwargs):
            self._handlers = {}

        def add_response(self, *args):
            pass

        def set_response_appearance(self, *args):
            pass

        def set_default_response(self, *args):
            pass

        def set_close_response(self, *args):
            pass

        def set_extra_child(self, *args):
            pass

        def connect(self, signal, callback):
            self._handlers[signal] = callback

        def present(self, *args):
            captured["dialog"] = self

    class _FakeDropDown:
        @staticmethod
        def new_from_strings(names):
            captured["names"] = names
            dd = MagicMock()
            dd.get_selected.return_value = 0
            return dd

    monkeypatch.setattr(win_mod.Adw, "MessageDialog", _FakeDialog)
    monkeypatch.setattr(win_mod.Gtk, "DropDown", _FakeDropDown)

    window = _make_window()
    window._key_manager = MagicMock()
    real_prompt = win_mod.AuthorizedKeysWindow._prompt_local_key_pick
    window._prompt_local_key_pick = real_prompt.__get__(window)

    window._prompt_local_key_pick([_key("k1")])
    response_cb = captured["dialog"]._handlers["response"]

    window._closing = True
    response_cb(None, "add")

    assert _ThreadSpy.instances == []


def test_callbacks_after_teardown_do_not_touch_widgets(monkeypatch, idle):
    monkeypatch.setattr(win_mod.threading, "Thread", _ThreadSpy)
    window = _make_window()
    window._key_manager = MagicMock()
    window._on_add_from_local()
    thread = _ThreadSpy.instances[0]
    window._key_manager.discover_keys.return_value = [_key()]
    window._add_button.set_sensitive.reset_mock()
    window._toast.reset_mock()

    window._closing = True
    thread.target(*thread.args)
    fn, args = idle[0]
    fn(*args)

    window._add_button.set_sensitive.assert_not_called()
    window._toast.assert_not_called()
    window._prompt_local_key_pick.assert_not_called()
    assert window._listing_keys is False


def test_operation_flags_reset_after_workers(monkeypatch, idle):
    monkeypatch.setattr(win_mod.threading, "Thread", _ThreadSpy)
    window = _make_window()
    window._key_manager = MagicMock()

    # Listing completes and resets the flag.
    window._on_add_from_local()
    thread = _ThreadSpy.instances.pop(0)
    window._key_manager.discover_keys.return_value = [_key()]
    thread.target(*thread.args)
    fn, args = idle.pop(0)
    fn(*args)
    assert window._listing_keys is False

    # Public-key read completes and resets the flag.
    window._start_public_key_read(_key())
    thread = _ThreadSpy.instances.pop(0)
    window._key_manager.read_public_key.return_value = "ssh-ed25519 AAAA ok\n"
    thread.target(*thread.args)
    fn, args = idle.pop(0)
    fn(*args)
    assert window._reading_public is False
    window._append_pubkey_text.assert_called_once_with("ssh-ed25519 AAAA ok\n")


# ---------------------------------------------------------------------------
# No direct .pub reads for daemon-discovered inventory
# ---------------------------------------------------------------------------
_IMPORT_FUNCS = frozenset(
    {
        "_on_add_from_local",
        "_list_keys_worker",
        "_on_local_keys_loaded",
        "_prompt_local_key_pick",
        "_start_public_key_read",
        "_read_public_worker",
        "_on_public_key_read_ok",
        "_on_public_key_read_failed",
    }
)


def _function_ids(func) -> set:
    ids = set()
    for node in ast.walk(func):
        if isinstance(node, ast.Name):
            ids.add(node.id)
        elif isinstance(node, ast.Attribute):
            ids.add(node.attr)
    return ids


def test_import_flow_never_opens_discovered_key_files():
    source = _SOURCE.read_text(encoding="utf-8")
    assert "def _append_pubkey_from_path" not in source, (
        "the local .pub read helper must be gone"
    )
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        if node.name not in _IMPORT_FUNCS:
            continue
        ids = _function_ids(node)
        assert not ({"open", "exists", "isfile", "_append_pubkey_from_path"} & ids), (
            f"{node.name} performs a local key-file operation"
        )
