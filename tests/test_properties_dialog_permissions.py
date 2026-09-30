"""Changing permissions from the file manager properties dialog."""

import os
from concurrent.futures import Future

from sshpilot.file_manager import permissions as perms
from sshpilot.file_manager import properties_dialog
from sshpilot.file_manager.common import FileEntry
from sshpilot.file_manager.properties_dialog import PropertiesDialog


def _dialog(entries, modes, *, current_path, manager=None):
    dialog = PropertiesDialog.__new__(PropertiesDialog)
    dialog._entries = list(entries)
    dialog._entry = dialog._entries[0]
    dialog._current_path = current_path
    dialog._sftp_manager = manager
    dialog._modes = list(modes)
    dialog._initial_modes = list(modes)
    dialog._syncing_permission_widgets = False
    dialog._synced = 0
    dialog._reported = []
    dialog._sync_permission_widgets = lambda: setattr(dialog, "_synced", dialog._synced + 1)
    dialog._report_permission_error = lambda entry, exc: dialog._reported.append((entry.name, exc))
    return dialog


class _Row:
    def __init__(self, selected=0, active=False):
        self.selected = selected
        self.active = active

    def get_selected(self):
        return self.selected

    def get_active(self):
        return self.active


class _Manager:
    def __init__(self, fail=()):
        self.calls = []
        self.fail = set(fail)

    def chmod(self, path, mode):
        self.calls.append((path, mode))
        future = Future()
        if path in self.fail:
            future.set_exception(PermissionError("Permission denied"))
        else:
            future.set_result(None)
        return future


def test_local_group_access_change_is_applied_with_chmod(tmp_path):
    target = tmp_path / "notes.txt"
    target.write_text("x")
    os.chmod(target, 0o640)
    dialog = _dialog(
        [FileEntry("notes.txt", False, 1, 0.0)], [0o100640], current_path=str(tmp_path)
    )
    values = perms.permission_choices(perms.PermissionClass.GROUP, False)

    dialog._on_access_selected(
        _Row(values.index(perms.READ | perms.WRITE)), None,
        perms.PermissionClass.GROUP, False, values,
    )

    assert os.stat(target).st_mode & 0o7777 == 0o660
    assert dialog._modes == [0o100660]
    assert dialog._synced == 1


def test_local_failure_reverts_and_reports(tmp_path, monkeypatch):
    def _denied(path, mode):
        raise PermissionError(1, "Operation not permitted", path)

    monkeypatch.setattr(properties_dialog.os, "chmod", _denied)
    dialog = _dialog(
        [FileEntry("root.txt", False, 1, 0.0)], [0o100644], current_path=str(tmp_path)
    )

    dialog._on_execution_toggled(_Row(active=True), None)

    assert dialog._modes == [0o100644]
    assert [name for name, _exc in dialog._reported] == ["root.txt"]


def test_remote_access_change_only_touches_matching_kind(monkeypatch):
    monkeypatch.setattr(properties_dialog.GLib, "idle_add", lambda fn: fn(), raising=False)
    manager = _Manager()
    dialog = _dialog(
        [FileEntry("docs", True, 0, 0.0), FileEntry("a.txt", False, 1, 0.0)],
        [0o40755, 0o100644],
        current_path="/srv",
        manager=manager,
    )
    values = perms.permission_choices(perms.PermissionClass.OTHERS, True)

    dialog._on_access_selected(
        _Row(values.index(0)), None, perms.PermissionClass.OTHERS, True, values
    )

    assert manager.calls == [("/srv/docs", 0o750)]
    assert dialog._modes == [0o40750, 0o100644]


def test_remote_partial_failure_reverts_only_the_failed_item(monkeypatch):
    monkeypatch.setattr(properties_dialog.GLib, "idle_add", lambda fn: fn(), raising=False)
    manager = _Manager(fail={"/srv/b.sh"})
    dialog = _dialog(
        [FileEntry("a.sh", False, 1, 0.0), FileEntry("b.sh", False, 1, 0.0)],
        [0o100644, 0o100644],
        current_path="/srv",
        manager=manager,
    )

    dialog._on_execution_toggled(_Row(active=True), None)

    assert sorted(manager.calls) == [("/srv/a.sh", 0o755), ("/srv/b.sh", 0o755)]
    assert dialog._modes == [0o100755, 0o100644]
    assert [name for name, _exc in dialog._reported] == ["b.sh"]
    assert dialog._synced == 1


def test_mixed_choice_restores_each_items_original_bits(monkeypatch):
    monkeypatch.setattr(properties_dialog.GLib, "idle_add", lambda fn: fn(), raising=False)
    manager = _Manager()
    dialog = _dialog(
        [FileEntry("a", False, 1, 0.0), FileEntry("b", False, 1, 0.0)],
        [0o100600, 0o100660],
        current_path="/srv",
        manager=manager,
    )
    who = perms.PermissionClass.GROUP
    values = perms.permission_choices(who, False) + [perms.INCONSISTENT]
    dialog._on_access_selected(_Row(values.index(0)), None, who, False, values)
    assert dialog._modes == [0o100600, 0o100600]

    dialog._on_access_selected(_Row(values.index(perms.INCONSISTENT)), None, who, False, values)

    assert dialog._modes == [0o100600, 0o100660]
    assert manager.calls[-1] == ("/srv/b", 0o660)


def test_unchanged_mode_sends_nothing(monkeypatch):
    manager = _Manager()
    dialog = _dialog(
        [FileEntry("run.sh", False, 1, 0.0)], [0o100755], current_path="/srv", manager=manager
    )

    dialog._on_execution_toggled(_Row(active=True), None)

    assert manager.calls == []
    assert dialog._synced == 0
