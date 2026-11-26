import types


class _StatefulAction:
    """Stands in for the view menu's Gio.SimpleAction (pane.show-hidden)."""

    def __init__(self, state=False):
        self.state = state
        self.set_calls = []

    def get_state(self):
        return types.SimpleNamespace(get_boolean=lambda: self.state)

    def set_state(self, variant):
        self.state = variant.get_boolean()
        self.set_calls.append(self.state)


def _make_minimal_pane(module):
    FilePane = module.FilePane
    pane = FilePane.__new__(FilePane)
    pane._show_hidden = False
    pane._menu_actions = {"show-hidden": _StatefulAction()}
    pane._filter_calls = []

    def _apply_entry_filter(*, preserve_selection=True):
        pane._filter_calls.append(preserve_selection)

    pane._apply_entry_filter = _apply_entry_filter  # type: ignore[assignment]
    return pane


def test_show_hidden_toggle_affects_only_current_pane(load_file_manager_window, monkeypatch):
    module = load_file_manager_window()
    monkeypatch.setattr(
        module.GLib, "Variant",
        types.SimpleNamespace(new_boolean=lambda v: types.SimpleNamespace(get_boolean=lambda: v)),
        raising=False,
    )
    left = _make_minimal_pane(module)
    right = _make_minimal_pane(module)

    left.set_show_hidden(True)

    assert left._show_hidden is True
    assert right._show_hidden is False
    # The view menu's check item follows (Ctrl+H and the menu both land here).
    assert left._menu_actions["show-hidden"].set_calls == [True]
    assert right._menu_actions["show-hidden"].set_calls == []
    assert left._filter_calls == [True]
    assert right._filter_calls == []


def test_file_manager_window_no_global_show_hidden(load_file_manager_window):
    module = load_file_manager_window()
    FileManagerWindow = module.FileManagerWindow

    assert not hasattr(FileManagerWindow, "set_show_hidden")
