"""Which groups a delete acts on, and when the toolbar offers it.

Deleting groups is a batch action, so two things have to line up with the
selection: the trash button in the group toolbar must be live for any number
of real groups (but never for a virtual tag row, which owns nothing to
delete), and ``_get_target_group_rows`` must hand the action exactly the rows
the user was looking at — the whole multi-selection when the context menu was
opened inside it, the clicked row alone when it was opened outside it.

Headless: the window methods are called unbound on a lightweight double, so
no display or real GTK widget is needed.
"""

from types import SimpleNamespace

import pytest

try:
    import sshpilot.window as window_module
    from sshpilot.window import MainWindow
except Exception:  # pragma: no cover - depends on GTK test stub state
    window_module = None
    MainWindow = None

pytestmark = pytest.mark.skipif(
    MainWindow is None,
    reason="GTK stubs unavailable or polluted by sibling tests",
)


class _Button:
    def __init__(self):
        self.sensitive = None
        self.visible = None

    def set_sensitive(self, value):
        self.sensitive = bool(value)

    def set_visible(self, value):
        self.visible = bool(value)


class _GroupRow:
    def __init__(self, group_id, is_tag_group=False):
        self.group_id = group_id
        self.is_tag_group = is_tag_group


class _ConnectionRow:
    def __init__(self, connection):
        self.connection = connection


class _ListBox:
    def __init__(self, rows):
        self._rows = list(rows)

    def get_selected_rows(self):
        return list(self._rows)

    def get_selected_row(self):
        return self._rows[0] if self._rows else None


# The methods under test live on MainWindow, which cannot be instantiated
# headless; bind them onto a plain double instead.
_BORROWED = (
    "_get_selected_connection_rows",
    "_get_selected_group_rows",
    "_get_target_group_rows",
)


def _window(selected_rows):
    window = SimpleNamespace(
        connection_list=_ListBox(selected_rows),
        delete_button=_Button(),
        copy_key_button=_Button(),
        scp_button=_Button(),
        manage_files_button=_Button(),
        system_terminal_button=_Button(),
        rename_group_button=_Button(),
        delete_group_button=_Button(),
        edit_button=_Button(),
        toolbars=[],
        _context_menu_group_row=None,
        _context_menu_group_rows=None,
    )
    window._set_sidebar_selection_toolbar = window.toolbars.append
    for name in _BORROWED:
        setattr(window, name, getattr(MainWindow, name).__get__(window))
    return window


def _select(window):
    MainWindow.on_connection_selected(window, None, None)


def _target_rows(window, prefer_context=False):
    return MainWindow._get_target_group_rows(window, prefer_context=prefer_context)


@pytest.fixture(autouse=True)
def no_file_manager_gating(monkeypatch):
    """The group branch only consults this for the file-manager button."""
    monkeypatch.setattr(
        window_module, "should_hide_file_manager_options", lambda: False
    )


# --- toolbar ---------------------------------------------------------------


class TestDeleteButtonSensitivity:
    def test_one_group_enables_delete_and_rename(self):
        window = _window([_GroupRow("a")])

        _select(window)

        assert window.toolbars == ["group"]
        assert window.delete_group_button.sensitive is True
        assert window.rename_group_button.sensitive is True

    def test_several_groups_still_enable_delete(self):
        window = _window([_GroupRow("a"), _GroupRow("b"), _GroupRow("c")])

        _select(window)

        assert window.delete_group_button.sensitive is True

    def test_several_groups_disable_rename(self):
        """Rename edits one name; only delete became a batch action."""
        window = _window([_GroupRow("a"), _GroupRow("b")])

        _select(window)

        assert window.rename_group_button.sensitive is False

    def test_a_tag_row_cannot_be_deleted(self):
        window = _window([_GroupRow("tag::prod", is_tag_group=True)])

        _select(window)

        assert window.delete_group_button.sensitive is False

    def test_a_tag_row_mixed_into_the_selection_disables_delete(self):
        """Better to refuse than to silently delete a subset of the selection."""
        window = _window([_GroupRow("a"), _GroupRow("tag::prod", is_tag_group=True)])

        _select(window)

        assert window.delete_group_button.sensitive is False

    def test_groups_mixed_with_connections_disable_delete(self):
        """A mixed selection has no single subject; the toolbar goes blank."""
        window = _window([_GroupRow("a"), _ConnectionRow(SimpleNamespace())])

        _select(window)

        assert window.toolbars == ["empty"]
        assert window.delete_group_button.sensitive is False

    def test_an_empty_selection_disables_delete(self):
        window = _window([])

        _select(window)

        assert window.toolbars == ["empty"]
        assert window.delete_group_button.sensitive is False


# --- what the action is handed ---------------------------------------------


class TestTargetGroupRows:
    def test_the_whole_selection_is_targeted(self):
        rows = [_GroupRow("a"), _GroupRow("b")]
        window = _window(rows)

        assert _target_rows(window) == rows

    def test_tag_rows_are_never_targeted(self):
        real = _GroupRow("a")
        window = _window([real, _GroupRow("tag::prod", is_tag_group=True)])

        assert _target_rows(window) == [real]

    def test_a_context_row_inside_the_selection_keeps_the_selection(self):
        """Right-clicking one of several selected groups deletes them all."""
        rows = [_GroupRow("a"), _GroupRow("b")]
        window = _window(rows)
        window._context_menu_group_row = rows[1]

        assert _target_rows(window, prefer_context=True) == rows

    def test_a_context_row_outside_the_selection_wins(self):
        """Right-clicking elsewhere narrows to that row, as GTK reselects it."""
        selected = _GroupRow("a")
        clicked = _GroupRow("b")
        window = _window([selected])
        window._context_menu_group_row = clicked

        assert _target_rows(window, prefer_context=True) == [clicked]

    def test_a_context_tag_row_is_ignored(self):
        selected = _GroupRow("a")
        window = _window([selected])
        window._context_menu_group_row = _GroupRow("tag::prod", is_tag_group=True)

        assert _target_rows(window, prefer_context=True) == [selected]

    def test_the_menu_snapshot_wins_over_a_changed_selection(self):
        """The popover outlives the menu; the batch targets what it opened on."""
        snapshot = [_GroupRow("a"), _GroupRow("b")]
        window = _window([_GroupRow("later")])
        window._context_menu_group_rows = snapshot

        assert _target_rows(window, prefer_context=True) == snapshot

    def test_the_toolbar_button_drops_a_stale_menu_snapshot(self):
        rows = [_GroupRow("a"), _GroupRow("b")]
        window = _window(rows)
        window._context_menu_group_rows = [_GroupRow("stale")]
        window._context_menu_group_row = _GroupRow("also-stale")
        targeted = []
        window.on_delete_group_action = lambda *_args: targeted.extend(
            _target_rows(window, prefer_context=True)
        )

        MainWindow.on_delete_group_clicked(window, None)

        assert targeted == rows

    def test_the_toolbar_button_ignores_a_tag_only_selection(self):
        window = _window([_GroupRow("tag::prod", is_tag_group=True)])
        called = []
        window.on_delete_group_action = lambda *_args: called.append(True)

        MainWindow.on_delete_group_clicked(window, None)

        assert called == []
