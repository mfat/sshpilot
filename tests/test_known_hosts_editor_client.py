"""Focused GTK tests for the client-backed known-hosts editor.

Runs against the stubbed ``gi`` environment (conftest) with permissive widget
dummies, proving load/remove/save all route through ``KnownHostsController``
and never fall back to local filesystem I/O.
"""
from __future__ import annotations

import sys
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
    """Permissive GTK stub: unknown methods are no-ops; state for assertions."""

    def __init__(self, *args, **kwargs):
        self.children = []
        self._text = ""
        self._opacity = 1.0

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

    def list_known_hosts(self):
        self.listed += 1
        return KnownHostsSnapshot(
            revision=self.revision,
            entries=tuple(self.entries),
        )

    def remove_known_host_entries(self, request):
        self.removed += 1
        self.last_request = request
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


def _build_editor(client, on_saved=None, monkeypatch=None):
    """Build the window via __new__ and run its worker flow synchronously."""
    import sshpilot.known_hosts_editor as editor_mod

    editor = KnownHostsEditorWindow.__new__(KnownHostsEditorWindow)
    editor._on_saved = on_saved
    editor._all_entries = []
    editor.listbox = _Widget()
    editor.search_entry = _Widget()
    editor.close_calls = 0

    def _close():
        editor.close_calls += 1

    editor.close = _close

    from sshpilot.gtk.known_hosts_controller import KnownHostsController

    editor._controller = KnownHostsController(client)

    if monkeypatch is not None:
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


def test_load_comes_from_fake_client(monkeypatch):
    client = _FakeClient(entries=[_entry("a"), _entry("b")])
    editor = _build_editor(client, monkeypatch=monkeypatch)
    _install_icon_stub(monkeypatch)

    editor._load_entries()

    assert client.listed == 1
    assert [e.entry_id for e in editor._all_entries] == [
        KnownHostEntryId("a"),
        KnownHostEntryId("b"),
    ]


def test_duplicate_lines_remove_selected_id(monkeypatch):
    # Two rows with identical hostname/line but distinct IDs must stay
    # distinguishable: removal stages the row's own entry_id.
    client = _FakeClient(entries=[_entry("a"), _entry("b")])
    editor = _build_editor(client, monkeypatch=monkeypatch)
    _install_icon_stub(monkeypatch)
    editor._load_entries()
    assert len(editor.listbox.children) == 2

    second_row = editor.listbox.children[1]
    editor._on_remove_clicked(_Widget(), second_row)

    assert editor._controller.pending_entry_ids() == (KnownHostEntryId("b"),)
    assert second_row._entry_id == KnownHostEntryId("b")
    # Only the selected row's summary was dropped from the filter list.
    assert [e.entry_id for e in editor._all_entries] == [KnownHostEntryId("a")]


def test_save_sends_original_revision(monkeypatch):
    client = _FakeClient(entries=[_entry("a"), _entry("b")])
    saved = []
    editor = _build_editor(client, on_saved=lambda: saved.append(True), monkeypatch=monkeypatch)
    _install_icon_stub(monkeypatch)
    editor._load_entries()
    editor._on_remove_clicked(_Widget(), editor.listbox.children[0])

    editor._on_save_clicked(_Widget())

    assert client.removed == 1
    assert client.last_request.revision == "revision-1"
    assert client.last_request.entry_ids == (KnownHostEntryId("a"),)
    assert saved == [True]


def test_cancel_sends_nothing(monkeypatch):
    client = _FakeClient(entries=[_entry("a")])
    editor = _build_editor(client, monkeypatch=monkeypatch)
    _install_icon_stub(monkeypatch)
    editor._load_entries()

    editor._on_cancel_clicked(_Widget())

    assert client.removed == 0
    assert client.listed == 1  # load only; cancel never mutates


def test_stale_reloads_without_retrying(monkeypatch):
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

    # One failed save (no automatic retry) and a fresh reload happened.
    # The reload legitimately clears pending IDs: the old snapshot's IDs no
    # longer match the refreshed file.
    assert client.removed == 1
    assert client.listed == 2
    assert editor._controller.pending_entry_ids() == ()


def test_daemon_failure_performs_no_local_io(monkeypatch):
    import sshpilot.core.known_hosts.service as kh_service

    def _boom(*_a, **_k):
        raise AssertionError("local known-hosts I/O must not happen")

    monkeypatch.setattr(kh_service, "load_known_hosts", _boom)
    monkeypatch.setattr(kh_service, "save_known_hosts", _boom)

    client = _FakeClient(entries=[_entry("a")])
    client.remove_error = SshPilotError(ErrorCode.DAEMON_UNAVAILABLE, "no daemon")
    editor = _build_editor(client, monkeypatch=monkeypatch)
    _install_icon_stub(monkeypatch)
    editor._load_entries()
    editor._on_remove_clicked(_Widget(), editor.listbox.children[0])

    editor._on_save_clicked(_Widget())

    assert client.removed == 1
    assert editor._controller.pending_entry_ids() == (KnownHostEntryId("a"),)


def test_editor_has_no_local_known_hosts_imports():
    """AST guard: the editor must not import/call legacy local I/O helpers."""
    import ast
    from pathlib import Path

    source = Path(
        __file__).resolve().parents[1] / "src" / "sshpilot" / "known_hosts_editor.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))

    forbidden = {"get_ssh_dir", "load_known_hosts", "save_known_hosts"}
    used = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            used.add(node.id)
        elif isinstance(node, ast.Attribute):
            used.add(node.attr)
        elif isinstance(node, ast.ImportFrom):
            used.update(a.asname or a.name for a in node.names)
        elif isinstance(node, ast.Import):
            used.update(a.asname or a.name for a in node.names)

    assert not (forbidden & used), (
        f"known_hosts_editor.py still references {forbidden & used}"
    )
