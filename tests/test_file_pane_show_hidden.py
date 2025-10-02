from tests.test_file_pane_typeahead import _load_file_manager_window


class DummyToolbar:
    def __init__(self):
        self.states = []

    def set_show_hidden_state(self, state):
        self.states.append(state)


def _make_minimal_pane(module):
    FilePane = module.FilePane
    pane = FilePane.__new__(FilePane)
    pane.toolbar = DummyToolbar()
    pane._show_hidden = False
    pane._filter_calls = []

    def _apply_entry_filter(*, preserve_selection=True):
        pane._filter_calls.append(preserve_selection)

    pane._apply_entry_filter = _apply_entry_filter  # type: ignore[assignment]
    return pane


def test_show_hidden_toggle_affects_only_current_pane():
    module = _load_file_manager_window()
    left = _make_minimal_pane(module)
    right = _make_minimal_pane(module)

    assert left._show_hidden is False
    assert right._show_hidden is False

    left._on_toolbar_show_hidden_toggled(left.toolbar, True)

    assert left._show_hidden is True
    assert right._show_hidden is False
    assert left.toolbar.states == [True]
    assert right.toolbar.states == []
    assert left._filter_calls == [True]
    assert right._filter_calls == []


def test_file_manager_window_no_global_show_hidden():
    module = _load_file_manager_window()
    FileManagerWindow = module.FileManagerWindow

    assert not hasattr(FileManagerWindow, "set_show_hidden")
