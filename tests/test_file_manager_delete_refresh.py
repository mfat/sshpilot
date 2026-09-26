"""Remote listings after a delete must reflect it without a manual refresh.

Every upload finishing schedules its own refresh, and each listing is
serialized with every other SFTP command, so a burst of them both delays the
next delete and lands listings that predate it after its rows were hidden.
"""
import sys
import types
from unittest.mock import MagicMock


def _window():
    if "cairo" not in sys.modules:
        sys.modules["cairo"] = types.SimpleNamespace()
    from sshpilot.file_manager_window import FileManagerWindow

    win = FileManagerWindow.__new__(FileManagerWindow)
    win._left_pane = MagicMock(name="left")
    win._right_pane = MagicMock(name="right")
    win._right_pane._is_remote = True
    win._right_pane._current_path = "/root/docs"
    win._pending_paths = {win._left_pane: None, win._right_pane: None}
    win._pending_highlights = {win._left_pane: None, win._right_pane: None}
    win._loading_toast_timeouts = {win._left_pane: None, win._right_pane: None}
    win._refreshing_panes = set()
    win._is_disposed = False
    win._manager = MagicMock(name="manager")
    return win


def _land(win, path="/root/docs"):
    win._on_directory_loaded(win._manager, path, [])


def test_a_listing_lands_before_any_refresh_was_requested():
    """The first listing of a session arrives before any refresh ran."""
    win = _window()
    win._pending_paths[win._right_pane] = "/root/docs"

    _land(win)

    win._right_pane.show_entries.assert_called_once_with("/root/docs", [])
    win._manager.listdir.assert_not_called()


def test_a_burst_of_refreshes_costs_two_listings():
    win = _window()
    pane = win._right_pane

    for _ in range(8):  # one per finished upload
        win._force_refresh_pane(pane)
    assert win._manager.listdir.call_count == 1

    _land(win)  # the in-flight listing may predate some of those uploads
    assert win._manager.listdir.call_count == 2

    _land(win)
    assert win._manager.listdir.call_count == 2
    assert win._pending_paths[pane] is None


def test_a_refresh_that_fails_to_send_does_not_block_later_ones():
    win = _window()
    pane = win._right_pane
    win._manager.listdir.side_effect = [RuntimeError("boom"), None]

    win._force_refresh_pane(pane)
    win._force_refresh_pane(pane)

    assert win._manager.listdir.call_count == 2


def test_a_successful_remote_delete_releases_rows_and_relists():
    win = _window()
    pane = win._right_pane

    win._on_all_deletes_complete(
        pane,
        "/root/docs",
        [],
        2,
        success_count=2,
        used_progress_dialog=True,
        held_names=["a.md", "b.md"],
        held_path="/root/docs",
    )

    pane.release_removed_entries.assert_called_once_with(["a.md", "b.md"], "/root/docs")
    win._manager.listdir.assert_called_once_with("/root/docs")


def test_the_relist_after_a_delete_waits_behind_an_older_listing():
    win = _window()
    pane = win._right_pane
    win._force_refresh_pane(pane)  # requested before the delete

    win._on_all_deletes_complete(
        pane, "/root/docs", [], 1, success_count=1, used_progress_dialog=True, held_names=["a.md"]
    )
    assert win._manager.listdir.call_count == 1

    _land(win)  # the stale listing lands; a fresh one follows it
    assert win._manager.listdir.call_count == 2
