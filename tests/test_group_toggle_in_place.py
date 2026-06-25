import types

import sshpilot.sidebar as sidebar_module
import sshpilot.window as window_module
from sshpilot.sidebar import (
    GroupRow,
    _group_drop_zone,
    _resolve_group_color_by_id,
    _row_at_y_or_nearest,
    _would_create_group_cycle,
)
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


def test_build_grouped_list_registers_and_hides_nested_groups(monkeypatch):
    """A nested child group is registered and hidden when the parent collapses."""
    created = {}

    class _FakeGroupRow:
        # Reuse the real visibility recursion under test.
        apply_descendant_visibility = GroupRow.apply_descendant_visibility

        def __init__(self, group_info, group_manager, connections_dict=None):
            self.group_info = group_info
            self.group_id = group_info["id"]
            self._member_rows = []
            self._child_group_rows = []
            self.visible = True
            created[self.group_id] = self

        def connect(self, *args, **kwargs):
            pass

        def set_indentation(self, level):
            self.indent = level

        def add_member_row(self, row):
            self._member_rows.append(row)

        def add_child_group_row(self, row):
            self._child_group_rows.append(row)

        def set_visible(self, visible):
            self.visible = bool(visible)

    monkeypatch.setattr(window_module, "GroupRow", _FakeGroupRow)

    window = MainWindow.__new__(MainWindow)
    window.group_manager = object()
    window.connection_list = []
    window.add_connection_row = (
        lambda conn, level, display_group_id=None: _VisibleStub()
    )

    hierarchy = [
        {
            "id": "parent",
            "expanded": True,
            "connections": ["c1"],
            "children": [
                {
                    "id": "child",
                    "expanded": False,
                    "connections": ["c2"],
                    "children": [],
                }
            ],
        }
    ]
    connections_dict = {"c1": object(), "c2": object()}

    returned = window._build_grouped_list(hierarchy, connections_dict, 0)

    parent = created["parent"]
    child = created["child"]

    # Top-level rows are returned; the child is registered under the parent.
    assert returned == [parent]
    assert parent._child_group_rows == [child]
    assert parent.indent == 0 and child.indent == 1
    assert len(parent._member_rows) == 1 and len(child._member_rows) == 1

    # Collapsing the parent hides the child group row and its members.
    parent.group_info["expanded"] = False
    parent.apply_descendant_visibility(True)
    assert child.visible is False
    assert child._member_rows[0].visible is False
    assert parent._member_rows[0].visible is False


def test_resolve_group_color_retain_own_else_inherit(monkeypatch):
    # The harness stubs GTK, so parse the colour string identity-style and
    # exercise the walk-up logic (retain own colour, else inherit parent's).
    import sshpilot.sidebar as sidebar_module

    monkeypatch.setattr(sidebar_module, "_parse_color", lambda value: value or None)

    class _FakeManager:
        groups = {
            "parent": {"id": "parent", "parent_id": None, "color": "red"},
            "child": {"id": "child", "parent_id": "parent", "color": None},
            "child_own": {"id": "child_own", "parent_id": "parent", "color": "green"},
            "orphan": {"id": "orphan", "parent_id": None, "color": None},
        }

    mgr = _FakeManager()

    # Colourless child inherits the parent's colour.
    assert _resolve_group_color_by_id(mgr, "child") == "red"
    # Child with its own colour keeps it.
    assert _resolve_group_color_by_id(mgr, "child_own") == "green"
    # No colour anywhere up the chain → None.
    assert _resolve_group_color_by_id(mgr, "orphan") is None


def test_group_row_indentation_honors_display_mode():
    """Nested group headers follow the fullwidth/nested Group Layout setting."""

    class _Box:
        def __init__(self):
            self.margin = None

        def set_margin_start(self, value):
            self.margin = value

    class _Config:
        def __init__(self, mode):
            self.mode = mode

        def get_setting(self, key, default=None):
            return self.mode

    class _Manager:
        def __init__(self, mode):
            self.config = _Config(mode)

    def _make(mode):
        row = GroupRow.__new__(GroupRow)
        row.group_manager = _Manager(mode)
        row._content = _Box()
        row._content_margin_base = 12
        row._indent_level = 0
        row._group_display_mode = None
        row.widget_margin = None
        row.set_margin_start = lambda value: setattr(row, "widget_margin", value)
        return row

    # nested → the whole header card shifts right; content margin unchanged.
    nested = _make("nested")
    nested.set_indentation(2)
    assert nested.widget_margin == 2 * 20
    assert nested._content.margin == 12

    # fullwidth → only the header content indents; row stays full width.
    full = _make("fullwidth")
    full.set_indentation(2)
    assert full.widget_margin == 0
    assert full._content.margin == 12 + 2 * 20

    # root level → no indentation regardless of mode.
    root = _make("nested")
    root.set_indentation(0)
    assert root.widget_margin == 0
    assert root._content.margin == 12


def test_would_create_group_cycle():
    class _Window:
        pass

    class _Manager:
        groups = {
            "a": {"id": "a", "parent_id": None},
            "b": {"id": "b", "parent_id": "a"},
        }

    window = _Window()
    window.group_manager = _Manager()

    assert _would_create_group_cycle(window, "a", "a") is True   # into itself
    assert _would_create_group_cycle(window, "a", "b") is True   # into descendant
    assert _would_create_group_cycle(window, "b", "a") is False  # valid nest
    assert _would_create_group_cycle(window, "a", None) is False  # to root


class _AllocRow:
    def __init__(self, y, height, group_id="dst"):
        self.group_id = group_id
        self._alloc = types.SimpleNamespace(y=y, height=height)

    def get_allocation(self):
        return self._alloc


def test_group_drop_zone_into_dominates_middle_half():
    row = _AllocRow(100, 40)  # spans y=100..140; quarters at 110 and 130

    # Outer quarters reorder.
    assert _group_drop_zone(row, 102) == "above"
    assert _group_drop_zone(row, 138) == "below"
    # Middle 50% nests, including the boundaries (not strictly inside the edges).
    assert _group_drop_zone(row, 110) == "into"
    assert _group_drop_zone(row, 120) == "into"
    assert _group_drop_zone(row, 130) == "into"
    # Degenerate allocation falls back to 'into'.
    assert _group_drop_zone(_AllocRow(0, 0), 0) == "into"


def test_row_at_y_or_nearest_bridges_margin_gap():
    sentinel = object()

    class _GapListBox:
        # y=50 is a margin gap; the real row sits at y+4.
        def get_row_at_y(self, y):
            return sentinel if y == 54 else None

    window = types.SimpleNamespace(connection_list=_GapListBox())
    assert _row_at_y_or_nearest(window, 50) is sentinel

    class _EmptyListBox:
        def get_row_at_y(self, y):
            return None

    window = types.SimpleNamespace(connection_list=_EmptyListBox())
    assert _row_at_y_or_nearest(window, 50) is None


def test_group_drop_follows_captured_indicator(monkeypatch):
    """The drop performs the highlighted action regardless of the drop y."""
    monkeypatch.setattr(sidebar_module, "_clear_drop_indicator", lambda w: None)
    monkeypatch.setattr(sidebar_module, "_hide_ungrouped_area", lambda w: None)
    monkeypatch.setattr(sidebar_module, "_stop_connection_autoscroll", lambda w: None)

    moved = []
    monkeypatch.setattr(
        sidebar_module,
        "_move_group",
        lambda w, gid, parent: (moved.append((gid, parent)), True)[1],
    )

    class _Manager:
        def __init__(self):
            self.groups = {
                "src": {"id": "src", "parent_id": None},
                "dst": {"id": "dst", "parent_id": None},
            }
            self.reordered = []

        def reorder_group(self, source, target, position):
            self.reordered.append((source, target, position))

    def _make_window(position):
        window = types.SimpleNamespace()
        window._drop_indicator_row = _AllocRow(0, 40, group_id="dst")
        window._drop_indicator_position = position
        window.group_manager = _Manager()
        window.rebuilt = []
        window.rebuild_connection_list = lambda: window.rebuilt.append(True)
        return window

    value = {"type": "group", "group_id": "src"}

    # 'on_group' highlight → nest, even though y is far past the target row.
    window = _make_window("on_group")
    assert sidebar_module._on_connection_list_drop(window, None, value, 0, 99999) is True
    assert moved == [("src", "dst")]
    assert window.group_manager.reordered == []

    # 'above' highlight → reorder as sibling (shared root parent, no reparent).
    moved.clear()
    window = _make_window("above")
    assert sidebar_module._on_connection_list_drop(window, None, value, 0, 99999) is True
    assert moved == []
    assert window.group_manager.reordered == [("src", "dst", "above")]
