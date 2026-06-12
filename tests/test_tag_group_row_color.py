"""Connection rows under virtual tag groups must not show group color."""

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


class _FakeGroupManager:
    """Always resolves to a group with a color."""

    def __init__(self):
        self.groups = {'g1': {'id': 'g1', 'color': '#ff0000', 'parent_id': None}}

    def resolve_display_group_id(self, nickname, context_group_id=None):
        return 'g1'


class _FakeRow:
    if sidebar is not None:
        _resolve_group_color = sidebar.ConnectionRow._resolve_group_color

    def __init__(self, in_tag_section):
        self.connection = type('C', (), {'nickname': 'host1'})()
        self.group_manager = _FakeGroupManager()
        self._group_id = None
        self._in_tag_section = in_tag_section


def test_tag_section_row_has_no_color(monkeypatch):
    # Even with a parseable color available, the tag-section guard wins.
    monkeypatch.setattr(sidebar, '_parse_color', lambda value: 'sentinel-rgba')
    assert _FakeRow(in_tag_section=True)._resolve_group_color() is None


def test_regular_row_still_resolves_color(monkeypatch):
    # The stubbed Gdk can't parse real colors, so substitute the parser.
    monkeypatch.setattr(sidebar, '_parse_color', lambda value: 'sentinel-rgba')
    assert _FakeRow(in_tag_section=False)._resolve_group_color() == 'sentinel-rgba'
