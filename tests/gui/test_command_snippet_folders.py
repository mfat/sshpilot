"""Real GTK: a Command Snippets folder can be renamed and deleted from the panel.

    SSHPILOT_GUI_TESTS=1 pytest -m gui tests/gui/test_command_snippet_folders.py
"""

from __future__ import annotations

import pytest

from tests._gui_harness import requires_gui

Gtk, Adw, Gio, GLib = requires_gui()

pytestmark = pytest.mark.gui


def _folder_rows(panel):
    from sshpilot.command_blocks import FolderRow

    rows = []
    child = panel._tree_list.get_first_child()
    while child is not None:
        if isinstance(child, FolderRow):
            rows.append(child)
        child = child.get_next_sibling()
    return rows


def _open_dialogs(h):
    from sshpilot.command_blocks import AddFolderDialog

    return [w for w in Gtk.Window.list_toplevels() if isinstance(w, AddFolderDialog) and w.get_visible()]


def test_folder_rename_dialog_prefills_and_saves(phase14_harness):
    h = phase14_harness
    win = h.gui.window
    panel = win._ensure_command_blocks_panel()
    assert panel is not None
    folder = panel.store.add_folder("Prod")
    cmd = panel.store.add_command("up", "uptime", folder_id=folder["id"])
    panel.refresh()
    # The header toggle only opens the panel over a terminal tab; show the
    # popup directly so the rows are mapped and can anchor a popover.
    win._command_popup.show()

    [row] = [r for r in _folder_rows(panel) if r.folder_id == folder["id"]]
    h.pump_until(lambda: row.get_mapped(), timeout=5.0, label="folder row mapped")
    panel._show_folder_context_menu(row, row._folder)
    h.pump(50)
    assert panel._tree_list.get_selected_row() is row
    panel._open_rename_folder_dialog(row._folder)
    h.pump_until(lambda: _open_dialogs(h), timeout=5.0, label="rename dialog shown")
    [dlg] = _open_dialogs(h)
    assert dlg.get_title() == "Rename Folder"
    assert dlg.create_btn.get_label() == "Rename"
    assert dlg.name_row.get_text() == "Prod"

    dlg.name_row.set_text("Production")
    dlg.create_btn.emit("clicked")
    h.pump_until(lambda: not _open_dialogs(h), timeout=5.0, label="rename dialog closed")

    [stored] = [f for f in panel.store.get_folders() if f["id"] == folder["id"]]
    assert stored["name"] == "Production"
    [row] = [r for r in _folder_rows(panel) if r.folder_id == folder["id"]]
    assert row._folder["name"] == "Production"

    panel._delete_folder(row._folder)
    h.pump(50)
    assert not [f for f in panel.store.get_folders() if f["id"] == folder["id"]]
    assert not [r for r in _folder_rows(panel) if r.folder_id == folder["id"]]
    [kept] = [c for c in panel.store.get_commands() if c["id"] == cmd["id"]]
    assert kept["folder_id"] is None
