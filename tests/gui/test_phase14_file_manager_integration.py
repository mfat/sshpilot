"""Phase 14 GTK file-manager production path."""

from __future__ import annotations

import pytest

from tests._gui_harness import requires_gui

Gtk, Adw, Gio, GLib = requires_gui()

pytestmark = pytest.mark.gui


def test_manage_files_daemon_backend_and_listing(phase14_harness):
    h = phase14_harness
    conn = h.add_password_connection("P14FM")
    h.open_file_manager(conn)
    evidence = h.file_manager_evidence()
    assert evidence["backend"] == "DaemonSftpManager", evidence
    assert evidence["fm_connected"] is True, evidence
    assert evidence["service_id"], evidence
    assert evidence["listing_model_populated"] is True, evidence
    assert evidence["row_count"] > 0, evidence
