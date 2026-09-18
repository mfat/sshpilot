"""Tests for dimmed, collapsible Groups / Ungrouped section headers."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import sshpilot.sidebar as sidebar_module
from sshpilot.sidebar import SectionHeaderRow


class _VisibleStub:
    def __init__(self):
        self.visible = None
        self.descendant_calls = []

    def set_visible(self, visible):
        self.visible = bool(visible)

    def apply_descendant_visibility(self, parent_visible=True):
        self.descendant_calls.append(bool(parent_visible))


class _Config:
    def __init__(self, settings=None):
        self._settings = dict(settings or {})

    def get_setting(self, key, default=None):
        return self._settings.get(key, default)

    def set_setting(self, key, value):
        self._settings[key] = value


def test_section_header_apply_descendant_visibility():
    header = SectionHeaderRow.__new__(SectionHeaderRow)
    header._expanded = True
    header._child_rows = [_VisibleStub(), _VisibleStub()]
    nested = _VisibleStub()
    header._child_rows[0].apply_descendant_visibility = nested.apply_descendant_visibility

    header.apply_descendant_visibility()
    assert [row.visible for row in header._child_rows] == [True, True]
    assert nested.descendant_calls == [True]

    header._expanded = False
    header.apply_descendant_visibility()
    assert [row.visible for row in header._child_rows] == [False, False]
    assert nested.descendant_calls[-1] is False


def test_section_header_toggle_persists_and_hides_children(monkeypatch):
    config = _Config()
    header = SectionHeaderRow.__new__(SectionHeaderRow)
    header.section_id = "groups"
    header._title = "Groups"
    header.config = config
    header._expanded = True
    header._child_rows = [_VisibleStub()]
    header.name_label = MagicMock()
    header.expand_button = MagicMock()
    header.emit = MagicMock()

    monkeypatch.setattr(sidebar_module, "set_accessible_name", lambda *a, **k: None)
    monkeypatch.setattr(sidebar_module, "set_accessible_expanded", lambda *a, **k: None)

    import sshpilot.icon_utils as icon_utils

    monkeypatch.setattr(icon_utils, "set_button_icon", MagicMock())

    SectionHeaderRow._toggle_expand(header)

    assert header._expanded is False
    assert header._child_rows[0].visible is False
    assert config.get_setting("ui.sidebar_section_expanded") == {"groups": False}
    header.emit.assert_called_once_with("section-toggled", "groups", False)


def test_section_header_loads_persisted_collapsed_state():
    config = _Config({"ui.sidebar_section_expanded": {"ungrouped": False}})
    header = SectionHeaderRow.__new__(SectionHeaderRow)
    header.section_id = "ungrouped"
    header.config = config
    assert SectionHeaderRow._load_expanded(header) is False


def test_group_section_end_index_stops_at_ungrouped_header():
    groups = SimpleNamespace(is_section_header=True, section_id="groups")
    group_row = SimpleNamespace(
        group_id="g1",
        _indent_level=0,
        is_tag_group=False,
    )
    ungrouped = SimpleNamespace(is_section_header=True, section_id="ungrouped")
    conn = SimpleNamespace(
        connection=object(),
        _group_id=None,
        _in_tag_section=False,
        is_section_header=False,
    )
    rows = [groups, group_row, ungrouped, conn]
    for i, row in enumerate(rows):
        row.get_next_sibling = (
            (lambda nxt: (lambda: nxt))(rows[i + 1] if i + 1 < len(rows) else None)
        )
        row.drop_placeholder = False
        row.ungrouped_area = False

    class _List:
        def get_first_child(self):
            return rows[0]

    window = SimpleNamespace(connection_list=_List())
    assert sidebar_module._group_section_end_index(window) == 2


def test_root_connections_start_index_skips_ungrouped_header(monkeypatch):
    group_row = SimpleNamespace(group_id="g1")
    subtree = SimpleNamespace()
    subtree.get_index = lambda: 2
    ungrouped_header = SimpleNamespace(
        is_section_header=True, section_id="ungrouped"
    )
    conn_row = SimpleNamespace(is_section_header=False)

    class _List:
        def get_row_at_index(self, idx):
            return {3: ungrouped_header, 4: conn_row}.get(idx)

    window = SimpleNamespace(
        connection_list=_List(),
        group_manager=SimpleNamespace(
            groups={"g1": {"parent_id": None, "order": 0}},
        ),
    )

    monkeypatch.setattr(
        sidebar_module, "_find_group_row_by_id", lambda w, gid: group_row
    )
    monkeypatch.setattr(
        sidebar_module, "_collect_group_subtree_rows", lambda row: [subtree]
    )
    assert sidebar_module._root_connections_start_index(window) == 4


def test_on_connection_activated_toggles_section_header():
    from sshpilot.window import MainWindow

    toggled = []
    row = SimpleNamespace(
        is_section_header=True,
        is_local_terminal_row=False,
        _toggle_expand=lambda: toggled.append(True),
    )
    window = MainWindow.__new__(MainWindow)
    MainWindow.on_connection_activated(window, None, row)
    assert toggled == [True]
