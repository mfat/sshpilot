"""A subgroup's children must follow the colour its group row shows.

Group A is red, group B is green. Moving B under A with "Use Group Color for
Child Rows" on turns B's group row red, so B's connection rows must turn red
too rather than keep B's own green. Moving B back out restores green, since
the inherited colour is never stored on B.
"""

import pytest

# Import manually instead of importorskip: when sibling tests have replaced
# the Gtk stub in sys.modules, this import raises AttributeError (not
# ImportError), which importorskip would report as a collection error.
try:
    import sshpilot.sidebar as sidebar
except Exception:  # pragma: no cover - depends on test execution order
    sidebar = None

pytestmark = pytest.mark.skipif(
    sidebar is None,
    reason="GTK stubs unavailable or polluted by sibling tests",
)

RED = '#ff0000'
GREEN = '#00ff00'
BLUE = '#0000ff'
MODES = ['fill', 'badge', 'bar', 'dot']


class _FakeConfig:
    def __init__(self, mode, color_children=True):
        self._settings = {
            'ui.group_color_child_rows': color_children,
            'ui.group_color_display': mode,
        }

    def get_setting(self, key, default=None):
        return self._settings.get(key, default)


class _FakeGroupManager:
    """Group A (red) and group B (green), both top-level to start with."""

    def __init__(self, config):
        self.config = config
        self.groups = {
            'A': {'id': 'A', 'color': RED, 'parent_id': None},
            'B': {'id': 'B', 'color': GREEN, 'parent_id': None},
        }
        self.memberships = {'host-in-b': 'B'}

    def place_group(self, group_id, parent_id):
        # Same effect as the store's place_group: only the parent changes.
        self.groups[group_id]['parent_id'] = parent_id

    def resolve_display_group_id(self, nickname, context_group_id=None):
        return self.memberships[nickname]


class _FakeGroupRow:
    if sidebar is not None:
        _apply_group_color_style = sidebar.GroupRow._apply_group_color_style

    def __init__(self, manager, group_id):
        self.group_manager = manager
        self.group_id = group_id


class _FakeConnectionRow:
    if sidebar is not None:
        _apply_group_color_style = sidebar.ConnectionRow._apply_group_color_style
        _resolve_group_color = sidebar.ConnectionRow._resolve_group_color

    def __init__(self, manager, nickname='host-in-b'):
        self.connection = type('C', (), {'nickname': nickname})()
        self.group_manager = manager
        self.config = manager.config
        self._group_id = manager.memberships[nickname]
        self._in_tag_section = False


@pytest.fixture
def paint(monkeypatch):
    """Restyle rows and return the colour each one ends up painted with."""

    def run(*rows):
        applied = {}

        def record(row, mode, rgba):
            # Bar mode clears the member row's bar, then paints its dot.
            if rgba is not None or row not in applied:
                applied[row] = rgba

        monkeypatch.setattr(sidebar, '_apply_row_color', record)
        monkeypatch.setattr(
            sidebar, '_update_color_dot', lambda row, rgba: record(row, 'dot', rgba))
        for row in rows:
            row._apply_group_color_style()
        return [applied[row] for row in rows]

    # The stubbed Gdk can't parse real colours; let the hex string stand in.
    monkeypatch.setattr(sidebar, '_parse_color', lambda value: value or None)
    return run


@pytest.mark.parametrize('mode', MODES)
def test_subgroup_children_follow_the_subgroup_row_color(paint, mode):
    manager = _FakeGroupManager(_FakeConfig(mode))
    manager.place_group('B', 'A')

    group_color, child_color = paint(
        _FakeGroupRow(manager, 'B'), _FakeConnectionRow(manager))

    # B's group row takes its parent's red...
    assert group_color == RED
    # ...so the connections inside B must be red as well, not B's own green.
    assert child_color == RED


@pytest.mark.parametrize('mode', MODES)
def test_subgroup_moved_back_out_restores_its_own_color(paint, mode):
    manager = _FakeGroupManager(_FakeConfig(mode))
    manager.place_group('B', 'A')
    assert paint(_FakeGroupRow(manager, 'B'), _FakeConnectionRow(manager)) == [RED, RED]

    manager.place_group('B', None)

    # The move out rebuilds the sidebar; fresh rows show B's own green again.
    assert paint(_FakeGroupRow(manager, 'B'), _FakeConnectionRow(manager)) == [GREEN, GREEN]
    # A keeps its red throughout.
    assert paint(_FakeGroupRow(manager, 'A')) == [RED]


def test_subgroup_without_own_color_is_uncolored_after_moving_out(paint):
    manager = _FakeGroupManager(_FakeConfig('fill'))
    manager.groups['B']['color'] = ''
    manager.place_group('B', 'A')
    assert paint(_FakeGroupRow(manager, 'B'), _FakeConnectionRow(manager)) == [RED, RED]

    manager.place_group('B', None)

    assert paint(_FakeGroupRow(manager, 'B'), _FakeConnectionRow(manager)) == [None, None]


def test_top_level_color_reaches_every_depth(paint):
    # C (blue) under B (green) under A (red): everything reads as A.
    manager = _FakeGroupManager(_FakeConfig('fill'))
    manager.groups['C'] = {'id': 'C', 'color': BLUE, 'parent_id': 'B'}
    manager.memberships['host-in-c'] = 'C'
    manager.place_group('B', 'A')

    colors = paint(
        _FakeGroupRow(manager, 'B'),
        _FakeGroupRow(manager, 'C'),
        _FakeConnectionRow(manager, 'host-in-b'),
        _FakeConnectionRow(manager, 'host-in-c'),
    )

    assert colors == [RED, RED, RED, RED]


def test_setting_off_keeps_each_groups_own_color(paint):
    manager = _FakeGroupManager(_FakeConfig('fill', color_children=False))
    manager.groups['C'] = {'id': 'C', 'color': '', 'parent_id': 'B'}
    manager.place_group('B', 'A')

    colors = paint(
        _FakeGroupRow(manager, 'A'),
        _FakeGroupRow(manager, 'B'),
        _FakeGroupRow(manager, 'C'),
        _FakeConnectionRow(manager),
    )

    # Own colour wins, a colourless group borrows the nearest one, and member
    # rows stay uncoloured because the setting is off.
    assert colors == [RED, GREEN, GREEN, None]
