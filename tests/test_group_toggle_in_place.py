import types

import sshpilot.sidebar as sidebar_module
import sshpilot.window as window_module
from sshpilot.sidebar import (
    GroupRow,
    _DROP_BAR_INSET_LEFT,
    _DROP_BAR_INSET_RIGHT,
    _DROP_BAR_THICKNESS,
    _apply_connection_dnd_in_place,
    _apply_group_dnd_in_place,
    _collect_group_subtree_rows,
    _drag_bar_geometry,
    _group_drop_zone,
    _group_in_nest_zone,
    _group_into_decision,
    _group_reorder_half,
    _group_reorder_zone,
    _listbox_reposition_row,
    _placeholder_insert_index,
    _resolve_group_color_by_id,
    _row_at_y_or_nearest,
    _sync_group_member_rows,
    _would_create_group_cycle,
    reset_connection_list_drag_session,
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


def test_drag_bar_geometry():
    width, height = 200, 10
    bar_x, bar_y, bar_w, bar_h, cap_cx, cap_cy, cap_r = _drag_bar_geometry(width, height)

    # Bar is inset from both edges and vertically centered.
    assert bar_x == _DROP_BAR_INSET_LEFT
    assert bar_w == width - _DROP_BAR_INSET_LEFT - _DROP_BAR_INSET_RIGHT
    assert bar_h == _DROP_BAR_THICKNESS
    assert bar_y == (height - bar_h) / 2

    # Cap is a node centered on the leading (left) end of the bar.
    assert cap_cx == bar_x
    assert cap_cy == height / 2
    assert cap_r > 0

    # Degenerate narrow widget clamps the bar width to non-negative.
    _, _, narrow_w, _, _, _, _ = _drag_bar_geometry(4, 10)
    assert narrow_w == 0


def test_group_into_decision():
    groups = {
        "a": {"id": "a", "parent_id": None},
        "b": {"id": "b", "parent_id": None},
        "c": {"id": "c", "parent_id": "b"},   # c nested in b
    }
    # Dragging c over its current parent b → reorder out (not a no-op nest).
    assert _group_into_decision(groups, "c", "b") == "reorder"
    # Dragging c over an unrelated root group a → a real nest.
    assert _group_into_decision(groups, "c", "a") == "nest"
    # Dragging b over its own descendant c → cycle → invalid.
    assert _group_into_decision(groups, "b", "c") == "invalid"
    # Onto itself → invalid.
    assert _group_into_decision(groups, "c", "c") == "invalid"


def test_group_reorder_half():
    row = _AllocRow(100, 40, header_height=40)  # header spans y=100..140
    assert _group_reorder_half(row, 105) == "above"   # upper half
    assert _group_reorder_half(row, 135) == "below"   # lower half
    # Past the header still resolves (no 'into'); lower half → below.
    assert _group_reorder_half(row, 180) == "below"


def test_placeholder_insert_index():
    # 'above' lands at the target's own slot; 'below' one past it.
    assert _placeholder_insert_index(3, "above") == 3
    assert _placeholder_insert_index(3, "below") == 4
    assert _placeholder_insert_index(0, "above") == 0
    assert _placeholder_insert_index(0, "below") == 1


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
    def __init__(self, y, height, group_id="dst", header_height=None):
        self.group_id = group_id
        self._alloc = types.SimpleNamespace(y=y, height=height)
        h = height if header_height is None else header_height
        # Stable header box, independent of the (possibly inflated) row height.
        self._content = types.SimpleNamespace(
            get_allocation=lambda: types.SimpleNamespace(y=y, height=h)
        )

    def get_allocation(self):
        return self._alloc


def test_group_reorder_zone():
    row = _AllocRow(100, 120, header_height=40)  # header spans y=100..140

    # Whole header splits by half for reorder.
    assert _group_reorder_zone(row, 110) == "above"   # rel=10
    assert _group_reorder_zone(row, 125) == "below"   # rel=25
    # Past the header is a sibling seam unless nest mode is active.
    assert _group_reorder_zone(row, 145) == "below"
    assert _group_reorder_zone(row, 145, nesting_active=True) == "into"

    collapsed = _AllocRow(100, 40, header_height=40)
    assert _group_reorder_zone(collapsed, 120) == "below"   # rel=20, past header


def test_group_in_nest_zone():
    row = _AllocRow(100, 40, header_height=40)
    assert _group_in_nest_zone(row, 120) is True    # rel=20, middle third
    assert _group_in_nest_zone(row, 110) is False   # rel=10, top third
    assert _group_in_nest_zone(row, 135) is False   # rel=35, bottom third
    assert _group_in_nest_zone(row, 145) is False   # past header


def test_group_drop_zone_delegates_to_reorder_zone():
    row = _AllocRow(100, 40, header_height=40)
    assert _group_drop_zone(row, 110) == _group_reorder_zone(row, 110)


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


def test_group_motion_shows_reorder_gap_on_header(monkeypatch):
    """Dragging a group over another group's header top/bottom shows a reorder gap."""
    shown = []
    monkeypatch.setattr(
        sidebar_module,
        "_show_drop_indicator",
        lambda w, row, position: shown.append((row.group_id, position)),
    )
    monkeypatch.setattr(sidebar_module, "_show_drop_indicator_on_group", lambda w, r: None)
    monkeypatch.setattr(sidebar_module, "_show_ungrouped_area", lambda w: None)
    monkeypatch.setattr(sidebar_module, "_update_connection_autoscroll", lambda w, y: None)
    monkeypatch.setattr(sidebar_module.GLib, "get_monotonic_time", lambda: 100_000)

    row = _AllocRow(100, 40, group_id="b", header_height=40)
    row.is_tag_group = False
    row.show_drop_indicator = lambda top: None
    row.hide_drop_indicators = lambda: None

    class _Manager:
        groups = {
            "a": {"id": "a", "parent_id": None},
            "b": {"id": "b", "parent_id": None},
            "c": {"id": "c", "parent_id": None},
        }

    window = types.SimpleNamespace()
    window._dragged_group_id = "c"
    window._drag_in_progress = True
    window._drop_indicator_row = None
    window._drop_indicator_position = None
    window.group_manager = _Manager()
    window.connection_list = types.SimpleNamespace(
        set_selection_mode=lambda mode: None,
        get_row_at_y=lambda y: row,
    )

    sidebar_module._on_connection_list_motion(window, None, 0, 110)

    assert shown == [("b", "above")]


def test_group_motion_shows_nest_on_header_center(monkeypatch):
    """Dragging a group over the middle third of a header shows Add to Group."""
    nested = []
    monkeypatch.setattr(
        sidebar_module,
        "_show_drop_indicator_on_group",
        lambda w, row: nested.append(row.group_id),
    )
    monkeypatch.setattr(sidebar_module, "_show_drop_indicator", lambda w, row, pos: None)
    monkeypatch.setattr(sidebar_module, "_show_ungrouped_area", lambda w: None)
    monkeypatch.setattr(sidebar_module, "_update_connection_autoscroll", lambda w, y: None)
    monkeypatch.setattr(sidebar_module.GLib, "get_monotonic_time", lambda: 100_000)

    row = _AllocRow(100, 40, group_id="b", header_height=40)
    row.is_tag_group = False
    row.show_drop_indicator = lambda top: None
    row.hide_drop_indicators = lambda: None

    class _Manager:
        groups = {
            "a": {"id": "a", "parent_id": None},
            "b": {"id": "b", "parent_id": None},
            "c": {"id": "c", "parent_id": None},
        }

    window = types.SimpleNamespace()
    window._dragged_group_id = "c"
    window._drag_in_progress = True
    window._drop_indicator_row = None
    window._drop_indicator_position = None
    window.group_manager = _Manager()
    window.connection_list = types.SimpleNamespace(
        set_selection_mode=lambda mode: None,
        get_row_at_y=lambda y: row,
    )

    sidebar_module._on_connection_list_motion(window, None, 0, 120)

    assert nested == ["b"]


def test_group_drop_ignores_connection_target(monkeypatch):
    """A group drop whose target is a connection row does nothing."""
    monkeypatch.setattr(sidebar_module, "_clear_drop_indicator", lambda w: None)
    monkeypatch.setattr(sidebar_module, "_hide_ungrouped_area", lambda w: None)
    monkeypatch.setattr(sidebar_module, "_stop_connection_autoscroll", lambda w: None)

    moved = []
    monkeypatch.setattr(
        sidebar_module,
        "_move_group",
        lambda w, gid, parent: (moved.append((gid, parent)), True)[1],
    )

    class _ConnRow:
        connection = object()

    conn_row = _ConnRow()
    monkeypatch.setattr(sidebar_module, "_row_at_y_or_nearest", lambda w, y: conn_row)

    class _Manager:
        def __init__(self):
            self.groups = {"src": {"id": "src", "parent_id": None}}
            self.reordered = []

        def reorder_group(self, *args):
            self.reordered.append(args)

    window = types.SimpleNamespace()
    window._drop_indicator_row = conn_row
    window._drop_indicator_position = None
    window.group_manager = _Manager()
    window.rebuilt = []
    window.rebuild_connection_list = lambda: window.rebuilt.append(True)

    value = {"type": "group", "group_id": "src"}
    assert sidebar_module._on_connection_list_drop(window, None, value, 0, 50) is False
    assert moved == []
    assert window.group_manager.reordered == []
    assert window.rebuilt == []


def test_reset_drag_session_clears_leaked_group_id(monkeypatch):
    monkeypatch.setattr(sidebar_module, "_clear_drop_indicator", lambda w: None)
    monkeypatch.setattr(sidebar_module, "_stop_connection_autoscroll", lambda w: None)

    class _List:
        def __init__(self):
            self.selection_mode = None

        def set_selection_mode(self, mode):
            self.selection_mode = mode

    window = types.SimpleNamespace()
    window._dragged_group_id = "leaked"
    window._ungrouped_area_visible = True
    window._ungrouped_area_row = None
    window._drag_in_progress = True
    window.connection_list = _List()

    reset_connection_list_drag_session(window)

    assert not hasattr(window, "_dragged_group_id")
    assert window._ungrouped_area_visible is False
    assert window._drag_in_progress is False


class _MockListBox:
    def __init__(self, rows):
        self._rows = list(rows)
        for idx, row in enumerate(self._rows):
            row._parent = self
            row._index = idx
            row._next = self._rows[idx + 1] if idx + 1 < len(self._rows) else None

    def get_first_child(self):
        return self._rows[0] if self._rows else None

    def remove(self, row):
        if row in self._rows:
            self._rows.remove(row)
            row._parent = None
            self._reindex()

    def insert(self, row, index):
        if row in self._rows:
            self._rows.remove(row)
        self._rows.insert(index, row)
        row._parent = self
        self._reindex()

    def _reindex(self):
        for idx, row in enumerate(self._rows):
            row._index = idx
            row._next = self._rows[idx + 1] if idx + 1 < len(self._rows) else None


class _MockRow:
    def __init__(self, label):
        self.label = label
        self._parent = None
        self._index = -1
        self._next = None

    def get_parent(self):
        return self._parent

    def get_index(self):
        return self._index

    def get_next_sibling(self):
        return self._next


def test_collect_group_subtree_rows_depth_first():
    parent = _MockRow("parent")
    parent.group_id = "p"
    parent._member_rows = [_MockRow("m1"), _MockRow("m2")]
    child = _MockRow("child")
    child.group_id = "c"
    child._member_rows = [_MockRow("m3")]
    child._child_group_rows = []
    parent._child_group_rows = [child]

    labels = [row.label for row in _collect_group_subtree_rows(parent)]
    assert labels == ["parent", "m1", "m2", "child", "m3"]


def test_listbox_reposition_row_moves_widget():
    rows = [_MockRow("a"), _MockRow("b"), _MockRow("c")]
    listbox = _MockListBox(rows)

    _listbox_reposition_row(listbox, rows[2], 0)

    assert [row.label for row in listbox._rows] == ["c", "a", "b"]


def test_sync_group_member_rows_reorders_without_rebuild():
    group_row = _MockRow("group")
    group_row.group_id = "g1"
    group_row._indent_level = 0
    group_row._member_rows = []

    conn_a = types.SimpleNamespace(nickname="a")
    conn_b = types.SimpleNamespace(nickname="b")
    row_a = _MockRow("row-a")
    row_a.connection = conn_a
    row_b = _MockRow("row-b")
    row_b.connection = conn_b
    row_a.set_display_group_id = lambda gid: None
    row_b.set_display_group_id = lambda gid: None
    row_a.set_indentation = lambda level: None
    row_b.set_indentation = lambda level: None

    group_row._member_rows = [row_a, row_b]
    listbox = _MockListBox([group_row, row_b, row_a])

    class _Manager:
        groups = {"g1": {"connections": ["a", "b"]}}

        def get_connection_group(self, nickname):
            return "g1"

    class _ConnManager:
        @staticmethod
        def find_connection_by_nickname(nickname):
            return {"a": conn_a, "b": conn_b}[nickname]

    window = types.SimpleNamespace()
    window._sidebar_view = "hosts"
    window.search_entry = types.SimpleNamespace(get_text=lambda: "")
    window.group_manager = _Manager()
    window.connection_manager = _ConnManager()
    window.connection_list = listbox
    window._rows_for_connection = lambda conn: [row_a if conn is conn_a else row_b]

    assert _sync_group_member_rows(window, "g1") is True
    assert [row.label for row in listbox._rows] == ["group", "row-a", "row-b"]
    assert [row.connection.nickname for row in group_row._member_rows] == ["a", "b"]


def test_connection_drop_skips_rebuild_when_inplace_succeeds(monkeypatch):
    monkeypatch.setattr(sidebar_module, "_clear_drop_indicator", lambda w: None)
    monkeypatch.setattr(sidebar_module, "_hide_ungrouped_area", lambda w: None)
    monkeypatch.setattr(sidebar_module, "_stop_connection_autoscroll", lambda w: None)

    applied = []
    monkeypatch.setattr(
        sidebar_module,
        "_apply_connection_dnd_in_place",
        lambda w, nicks: applied.append(nicks) or True,
    )

    class _Conn:
        nickname = "host-a"

    class _RefConn:
        nickname = "host-b"

    target_row = types.SimpleNamespace()
    target_row.connection = _RefConn()
    target_row._in_tag_section = False
    target_row.get_parent = lambda: object()

    class _Manager:
        def get_connection_group(self, nickname):
            return "g1"

        def move_connection(self, nickname, group_id):
            pass

        def reorder_connection_in_group(self, nickname, reference, position):
            pass

    window = types.SimpleNamespace()
    window._drop_indicator_row = target_row
    window._drop_indicator_position = "below"
    window._drag_in_progress = True
    window.group_manager = _Manager()
    window.connection_list = types.SimpleNamespace(
        set_selection_mode=lambda mode: None,
        get_row_at_y=lambda y: target_row,
    )
    window.rebuilt = []
    window.rebuild_connection_list = lambda: window.rebuilt.append(True)

    value = {"type": "connection", "connection_nickname": "host-a"}
    assert sidebar_module._on_connection_list_drop(window, None, value, 0, 50) is True
    assert applied == [["host-a"]]
    assert window.rebuilt == []


def test_group_drop_reorder_skips_rebuild_when_inplace_succeeds(monkeypatch):
    monkeypatch.setattr(sidebar_module, "_clear_drop_indicator", lambda w: None)
    monkeypatch.setattr(sidebar_module, "_hide_ungrouped_area", lambda w: None)
    monkeypatch.setattr(sidebar_module, "_stop_connection_autoscroll", lambda w: None)
    monkeypatch.setattr(sidebar_module, "_move_group", lambda *args: True)

    applied = []
    monkeypatch.setattr(
        sidebar_module,
        "_apply_group_dnd_in_place",
        lambda w, gid, **kwargs: applied.append((gid, kwargs)) or True,
    )

    class _Manager:
        def __init__(self):
            self.groups = {
                "src": {"id": "src", "parent_id": None},
                "dst": {"id": "dst", "parent_id": None},
            }

        def reorder_group(self, source, target, position):
            pass

    window = types.SimpleNamespace()
    window._drop_indicator_row = _AllocRow(0, 40, group_id="dst")
    window._drop_indicator_position = "above"
    window.group_manager = _Manager()
    window.rebuilt = []
    window.rebuild_connection_list = lambda: window.rebuilt.append(True)

    value = {"type": "group", "group_id": "src"}
    assert sidebar_module._on_connection_list_drop(window, None, value, 0, 99999) is True
    assert applied == [("src", {"nested": False, "reparented": False})]
    assert window.rebuilt == []
