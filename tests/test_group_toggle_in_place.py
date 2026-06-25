from sshpilot.sidebar import GroupRow
from sshpilot.window import MainWindow


class _VisibleStub:
    def __init__(self):
        self.visible = None

    def set_visible(self, visible):
        self.visible = bool(visible)


def test_group_row_apply_descendant_visibility_recurses():
    parent = GroupRow.__new__(GroupRow)
    parent.group_info = {"expanded": True}
    parent._member_rows = [_VisibleStub(), _VisibleStub()]

    child = GroupRow.__new__(GroupRow)
    child.group_info = {"expanded": False}
    child._member_rows = [_VisibleStub()]
    child._child_group_rows = []
    child.set_visible = lambda visible: setattr(child, "visible", bool(visible))

    parent._child_group_rows = [child]

    parent.apply_descendant_visibility(True)

    assert [row.visible for row in parent._member_rows] == [True, True]
    assert child.visible is True
    assert child._member_rows[0].visible is False

    parent.group_info["expanded"] = False
    parent.apply_descendant_visibility(True)

    assert [row.visible for row in parent._member_rows] == [False, False]
    assert child.visible is False
    assert child._member_rows[0].visible is False


def test_group_toggle_updates_visibility_without_rebuild():
    selected = []

    class _ToggleStub:
        def __init__(self):
            self.calls = []

        def apply_descendant_visibility(self, visible):
            self.calls.append(bool(visible))

    row = _ToggleStub()
    row.group_id = "g1"

    window = MainWindow.__new__(MainWindow)
    window.connection_list = [row]
    window._select_only_row = lambda selected_row: selected.append(selected_row)
    window.rebuild_connection_list = lambda: (_ for _ in ()).throw(
        AssertionError("should not rebuild on group toggle")
    )

    window._on_group_toggled(row, "g1", False)

    assert row.calls == [True]
    assert selected == [row]


def test_group_activation_toggles_via_single_row_activated_path():
    called = []

    class _GroupRowStub:
        group_id = "g1"

        def _toggle_expand(self):
            called.append("toggle")

    window = MainWindow.__new__(MainWindow)
    window._return_to_tab_view_if_welcome = lambda: None

    window.on_connection_activated(None, _GroupRowStub())

    assert "toggle" in called
