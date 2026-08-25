"""Focused editor tests for known-hosts save races and busy-state handling.

Runs against the stubbed ``gi`` environment (conftest) with permissive widget
dummies. Focus: one in-flight operation at a time, disabled controls while
busy, stale handling with exactly one reload, and no-change saves that never
touch the daemon.
"""
from __future__ import annotations

import sys
import threading
import types


sys.modules.setdefault("cairo", types.ModuleType("cairo"))

from sshpilot.api.errors import ErrorCode, SshPilotError
from sshpilot.api.models.known_hosts import (
    KnownHostEntryId,
    KnownHostEntrySummary,
    KnownHostsMutationResult,
    KnownHostsSnapshot,
)
from sshpilot.known_hosts_editor import KnownHostsEditorWindow


class _Widget:
    """Permissive GTK stub: records sensitivity and opacity for assertions."""

    def __init__(self, *args, **kwargs):
        self.children = []
        self._text = ""
        self._opacity = 1.0
        self._sensitive = True
        self.sensitive_calls = []

    def __getattr__(self, name):
        def _method(*args, **kwargs):
            return None

        return _method

    def append(self, child):
        self.children.append(child)
        return None

    def connect(self, *args, **kwargs):
        return 1

    def get_text(self):
        return self._text

    def set_text(self, text):
        self._text = text
        return None

    def get_first_child(self):
        return None  # terminate the clear-loop in _display_entries

    def set_opacity(self, value):
        self._opacity = value
        return None

    def get_opacity(self):
        return self._opacity

    def set_sensitive(self, value):
        self._sensitive = bool(value)
        self.sensitive_calls.append(bool(value))
        return None

    def get_sensitive(self):
        return self._sensitive


class _SyncThread:
    """Runs ``threading.Thread`` bodies synchronously for deterministic tests."""

    def __init__(self, *args, target=None, daemon=None, **kwargs):
        self._target = target

    def start(self):
        self._target()


class _FakeClient:
    def __init__(self, entries=()):
        self.revision = "revision-1"
        self.entries = list(entries)
        self.listed = 0
        self.removed = 0
        self.last_request = None
        self.remove_error = None
        self.list_block = None  # optional Event to block load()
        self.remove_block = None  # optional Event to block remove()

    def list_known_hosts(self):
        self.listed += 1
        if self.list_block is not None:
            self.list_block.wait(timeout=5)
        return KnownHostsSnapshot(
            revision=self.revision,
            entries=tuple(self.entries),
        )

    def remove_known_host_entries(self, request):
        self.removed += 1
        self.last_request = request
        if self.remove_block is not None:
            self.remove_block.wait(timeout=5)
        if self.remove_error is not None:
            raise self.remove_error
        remaining = [
            e for e in self.entries if e.entry_id not in request.entry_ids
        ]
        result = KnownHostsMutationResult(
            revision="revision-2",
            removed_count=len(request.entry_ids),
            entries=tuple(remaining),
        )
        self.entries = remaining
        self.revision = result.revision
        return result


def _entry(entry_id: str, hostname: str = "example.test") -> KnownHostEntrySummary:
    return KnownHostEntrySummary(
        entry_id=KnownHostEntryId(entry_id),
        hostname=hostname,
        key_type="ssh-ed25519",
        display_line=f"{hostname} ssh-ed25519 AAAA{entry_id}",
    )


def _build_editor(client, on_saved=None, monkeypatch=None, *, real_threads=False):
    """Build the window via __new__ with stubbed widgets and worker flow."""
    import sshpilot.known_hosts_editor as editor_mod

    editor = KnownHostsEditorWindow.__new__(KnownHostsEditorWindow)
    editor._on_saved = on_saved
    editor._all_entries = []
    editor.listbox = _Widget()
    editor.search_entry = _Widget()
    editor.save_button = _Widget()
    editor._operation_in_flight = False
    editor._closed = False
    editor.close_calls = 0

    def _close():
        editor.close_calls += 1

    editor.close = _close

    from sshpilot.gtk.known_hosts_controller import KnownHostsController

    editor._controller = KnownHostsController(client)

    if monkeypatch is not None:
        if not real_threads:
            monkeypatch.setattr(editor_mod, "threading", types.SimpleNamespace(
                Thread=_SyncThread,
            ))
        monkeypatch.setattr(
            editor_mod.GLib,
            "idle_add",
            lambda fn, *a, **k: (fn(*a, **k), None)[1],
        )
        monkeypatch.setattr(
            editor_mod.GLib,
            "timeout_add",
            lambda *a, **k: (_run_until_false(a[-1]), 1)[1],
        )
        monkeypatch.setattr(
            editor_mod,
            "install_esc_to_close",
            lambda *_a, **_k: None,
        )
        monkeypatch.setattr(
            editor_mod,
            "install_search_esc",
            lambda *_a, **_k: None,
        )
        monkeypatch.setattr(
            editor_mod.Gtk,
            "ListBoxRow",
            _Widget,
        )
        monkeypatch.setattr(
            editor_mod.Gtk,
            "Box",
            _Widget,
        )
        monkeypatch.setattr(
            editor_mod.Gtk,
            "Label",
            _Widget,
        )
        monkeypatch.setattr(
            editor_mod.Gtk,
            "Align",
            types.SimpleNamespace(START=0, CENTER=3),
        )
        monkeypatch.setattr(
            editor_mod.Gtk,
            "Orientation",
            types.SimpleNamespace(VERTICAL=0, HORIZONTAL=1),
        )
    return editor


def _run_until_false(fn, *a, **k):
    while fn(*a, **k):
        pass
    return None


def _install_icon_stub(monkeypatch):
    import sshpilot.icon_utils

    monkeypatch.setattr(
        sshpilot.icon_utils,
        "new_button_from_icon_name",
        lambda *_a, **_k: _Widget(),
    )
    monkeypatch.setattr(
        sshpilot.icon_utils,
        "set_button_icon",
        lambda *_a, **_k: None,
    )


def _wait_until(predicate, timeout=2.0):
    deadline = threading.Timer(timeout, lambda: None)  # noqa: F841 - simple sleep loop
    import time

    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def test_save_before_initial_load_completes_starts_no_mutation(monkeypatch):
    client = _FakeClient(entries=[_entry("a")])
    client.list_block = threading.Event()  # initial load stays in flight
    editor = _build_editor(client, monkeypatch=monkeypatch, real_threads=True)
    _install_icon_stub(monkeypatch)

    # Initial load is running (busy).
    editor._load_entries()
    assert editor._operation_in_flight is True

    editor._on_save_clicked(_Widget())

    assert client.removed == 0
    assert client.listed == 1  # load started, save ignored while busy
    client.list_block.set()
    # Do not let the real worker outlive this test. Once monkeypatch teardown
    # restores the suite-wide GTK stubs, a late idle callback would render with
    # different widget classes and surface as an unhandled thread exception in
    # whichever test happens to run next.
    assert _wait_until(lambda: editor._operation_in_flight is False)
    assert editor.listbox.children


def test_double_click_save_starts_one_mutation(monkeypatch):
    client = _FakeClient(entries=[_entry("a")])
    client.remove_block = threading.Event()  # keep first save in flight
    editor = _build_editor(client, monkeypatch=monkeypatch, real_threads=True)
    _install_icon_stub(monkeypatch)
    editor._load_entries()
    # Wait for the load to fully finish (rows populated, controls re-enabled).
    assert _wait_until(
        lambda: client.listed == 1 and editor._operation_in_flight is False
    )
    assert editor.listbox.children  # rows rendered

    editor._on_remove_clicked(_Widget(), editor.listbox.children[0])
    editor._on_save_clicked(_Widget())  # first click: starts mutation
    assert _wait_until(lambda: client.removed == 1)

    editor._on_save_clicked(_Widget())  # second click while busy: ignored
    assert client.removed == 1

    client.remove_block.set()
    assert _wait_until(lambda: editor.close_calls == 1)
    assert client.removed == 1


def test_remove_controls_disabled_during_save(monkeypatch):
    client = _FakeClient(entries=[_entry("a"), _entry("b")])
    client.remove_block = threading.Event()  # save stays in flight
    editor = _build_editor(client, monkeypatch=monkeypatch, real_threads=True)
    _install_icon_stub(monkeypatch)
    editor._load_entries()
    # Wait for the load to fully finish before asserting the enabled state.
    assert _wait_until(
        lambda: client.listed == 1 and editor._operation_in_flight is False
    )
    assert editor.save_button.get_sensitive() is True

    editor._on_remove_clicked(_Widget(), editor.listbox.children[0])
    editor._on_save_clicked(_Widget())
    assert _wait_until(lambda: client.removed == 1)

    # While the save is in flight all controls are disabled.
    assert editor.save_button.get_sensitive() is False
    assert editor.listbox.get_sensitive() is False
    assert editor.search_entry.get_sensitive() is False
    assert editor._operation_in_flight is True

    client.remove_block.set()
    assert _wait_until(lambda: editor.close_calls == 1)


def test_normal_error_restores_controls(monkeypatch):
    client = _FakeClient(entries=[_entry("a")])
    client.remove_error = SshPilotError(ErrorCode.DAEMON_UNAVAILABLE, "no daemon")
    editor = _build_editor(client, monkeypatch=monkeypatch)
    _install_icon_stub(monkeypatch)
    editor._load_entries()
    editor._on_remove_clicked(_Widget(), editor.listbox.children[0])

    editor._on_save_clicked(_Widget())

    # Controls restored, staged removals kept, window still open.
    assert editor._operation_in_flight is False
    assert editor.save_button.get_sensitive() is True
    assert editor.listbox.get_sensitive() is True
    assert editor.search_entry.get_sensitive() is True
    assert editor.close_calls == 0
    assert editor._controller.pending_entry_ids() == (KnownHostEntryId("a"),)


def test_stale_result_starts_exactly_one_reload(monkeypatch):
    client = _FakeClient(entries=[_entry("a")])
    client.remove_error = SshPilotError(
        ErrorCode.STALE_EDITOR,
        "changed",
        retryable=True,
        details={"resource": "known_hosts"},
    )
    editor = _build_editor(client, monkeypatch=monkeypatch)
    _install_icon_stub(monkeypatch)
    editor._load_entries()
    editor._on_remove_clicked(_Widget(), editor.listbox.children[0])

    editor._on_save_clicked(_Widget())

    # Exactly one failed mutation and exactly one reload; no automatic retry.
    assert client.removed == 1
    assert client.listed == 2  # initial load + one stale reload
    assert editor.close_calls == 0  # stale does not close
    assert editor._operation_in_flight is False  # controls re-enabled after reload
    assert editor.save_button.get_sensitive() is True


def test_stale_keeps_controls_disabled_until_reload_finishes(monkeypatch):
    client = _FakeClient(entries=[_entry("a")])
    client.remove_error = SshPilotError(
        ErrorCode.STALE_EDITOR,
        "changed",
        retryable=True,
        details={"resource": "known_hosts"},
    )
    editor = _build_editor(client, monkeypatch=monkeypatch, real_threads=True)
    _install_icon_stub(monkeypatch)
    editor._load_entries()
    assert _wait_until(lambda: client.listed == 1 and editor._operation_in_flight is False)

    # Arm the reload block only now: the initial load already finished, so
    # only the stale-triggered reload will stall in flight.
    client.list_block = threading.Event()

    editor._on_remove_clicked(_Widget(), editor.listbox.children[0])
    editor._on_save_clicked(_Widget())
    assert _wait_until(lambda: client.removed == 1)
    # Reload is now blocked in flight; controls must stay disabled.
    assert _wait_until(lambda: client.listed == 2)
    assert editor._operation_in_flight is True
    assert editor.save_button.get_sensitive() is False

    client.list_block.set()
    assert _wait_until(lambda: editor._operation_in_flight is False)
    assert editor.save_button.get_sensitive() is True


def test_no_change_save_closes_without_mutation_rpc(monkeypatch):
    client = _FakeClient(entries=[_entry("a")])
    saved = []
    editor = _build_editor(
        client, on_saved=lambda: saved.append(True), monkeypatch=monkeypatch
    )
    _install_icon_stub(monkeypatch)
    editor._load_entries()

    editor._on_save_clicked(_Widget())

    assert client.removed == 0  # no mutation RPC for an unchanged save
    assert client.listed == 1  # load only
    assert saved == [True]
    assert editor.close_calls == 1


def test_cancel_during_blocked_initial_load(monkeypatch):
    client = _FakeClient(entries=[_entry("a")])
    client.list_block = threading.Event()  # initial load stays in flight
    editor = _build_editor(client, monkeypatch=monkeypatch, real_threads=True)
    _install_icon_stub(monkeypatch)
    editor._load_entries()
    assert _wait_until(lambda: client.listed == 1)

    editor._on_cancel_clicked(_Widget())  # close while the load is blocked
    assert editor._closed is True

    client.list_block.set()
    # The finished load must not touch widgets after closure.
    assert _wait_until(lambda: editor._operation_in_flight is False)
    assert editor.listbox.children == []
    assert editor.close_calls == 1


def test_cancel_during_blocked_stale_save(monkeypatch):
    client = _FakeClient(entries=[_entry("a")])
    client.remove_error = SshPilotError(
        ErrorCode.STALE_EDITOR,
        "changed",
        retryable=True,
        details={"resource": "known_hosts"},
    )
    editor = _build_editor(client, monkeypatch=monkeypatch, real_threads=True)
    _install_icon_stub(monkeypatch)
    editor._load_entries()
    assert _wait_until(
        lambda: client.listed == 1 and editor._operation_in_flight is False
    )
    editor._on_remove_clicked(_Widget(), editor.listbox.children[0])

    client.remove_block = threading.Event()  # keep the save in flight
    editor._on_save_clicked(_Widget())
    assert _wait_until(lambda: client.removed == 1)

    editor._on_cancel_clicked(_Widget())  # close while the save is blocked
    assert editor._closed is True

    client.remove_block.set()  # the stale error now surfaces after closure
    assert _wait_until(lambda: editor._operation_in_flight is False)
    assert client.listed == 1  # no stale reload after closure
    assert editor.close_calls == 1


def test_close_during_successful_save(monkeypatch):
    client = _FakeClient(entries=[_entry("a")])
    saved = []
    editor = _build_editor(
        client, on_saved=lambda: saved.append(True),
        monkeypatch=monkeypatch, real_threads=True,
    )
    _install_icon_stub(monkeypatch)
    editor._load_entries()
    assert _wait_until(
        lambda: client.listed == 1 and editor._operation_in_flight is False
    )
    editor._on_remove_clicked(_Widget(), editor.listbox.children[0])

    client.remove_block = threading.Event()  # keep the save in flight
    editor._on_save_clicked(_Widget())
    assert _wait_until(lambda: client.removed == 1)

    editor._on_close_request()  # window close-request fires
    assert editor._closed is True

    client.remove_block.set()
    assert _wait_until(lambda: editor._operation_in_flight is False)
    # A successful save after closure may call on_saved once, but must not
    # call close() again.
    assert saved == [True]
    assert editor.close_calls == 0


def test_no_client_work_after_closure(monkeypatch):
    client = _FakeClient(entries=[_entry("a")])
    editor = _build_editor(client, monkeypatch=monkeypatch)
    _install_icon_stub(monkeypatch)

    editor._closed = True
    editor._load_entries()
    editor._start_load_worker()
    editor._on_save_clicked(_Widget())
    editor._on_remove_clicked(_Widget(), _Widget())

    assert client.listed == 0
    assert client.removed == 0
    assert editor._operation_in_flight is False
    assert editor.close_calls == 0


def test_no_dialog_or_reload_after_closure(monkeypatch):
    client = _FakeClient(entries=[_entry("a")])
    client.remove_error = SshPilotError(
        ErrorCode.DAEMON_UNAVAILABLE, "no daemon"
    )
    editor = _build_editor(client, monkeypatch=monkeypatch, real_threads=True)
    _install_icon_stub(monkeypatch)
    errors = []
    editor._show_error = lambda heading, body: errors.append((heading, body))
    editor._load_entries()
    assert _wait_until(
        lambda: client.listed == 1 and editor._operation_in_flight is False
    )
    editor._on_remove_clicked(_Widget(), editor.listbox.children[0])

    client.remove_block = threading.Event()  # keep the save in flight
    editor._on_save_clicked(_Widget())
    assert _wait_until(lambda: client.removed == 1)

    editor._on_cancel_clicked(_Widget())
    client.remove_block.set()  # the daemon error now surfaces after closure
    assert _wait_until(lambda: editor._operation_in_flight is False)
    assert errors == []  # no dialog after closure
    assert client.listed == 1  # no reload after closure


def test_on_saved_runs_at_most_once(monkeypatch):
    client = _FakeClient(entries=[_entry("a")])
    saved = []
    editor = _build_editor(
        client, on_saved=lambda: saved.append(True),
        monkeypatch=monkeypatch, real_threads=True,
    )
    _install_icon_stub(monkeypatch)
    editor._load_entries()
    assert _wait_until(
        lambda: client.listed == 1 and editor._operation_in_flight is False
    )
    editor._on_remove_clicked(_Widget(), editor.listbox.children[0])

    client.remove_block = threading.Event()
    editor._on_save_clicked(_Widget())
    assert _wait_until(lambda: client.removed == 1)

    editor._on_close_request()
    client.remove_block.set()
    assert _wait_until(lambda: editor._operation_in_flight is False)
    # One save worker -> one _on_save_finished -> on_saved exactly once, even
    # though the window was closed before the result landed. The busy flag
    # prevented a second save from ever starting.
    assert saved == [True]
    assert editor.close_calls == 0  # no close() after closure
