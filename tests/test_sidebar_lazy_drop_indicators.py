"""Sidebar rows create their drop indicators the first time a drag shows them.

Rows are built in bulk and only a few are ever dropped on, so neither
indicator exists until it is needed: the top one then goes before every other
child of the row's box, the bottom one after them, and later drags reuse it.
"""

import types

import pytest

import sshpilot.sidebar as sidebar_module


class _Indicator:
    def __init__(self):
        self.visible = False

    def set_visible(self, visible):
        self.visible = visible

    def get_next_sibling(self):
        return self.next_sibling


class _Box:
    def __init__(self, *children):
        self.children = list(children)

    def prepend(self, child):
        self.children.insert(0, child)

    def append(self, child):
        self.children.append(child)

    def get_first_child(self):
        child = self.children[0]
        if isinstance(child, _Indicator):
            child.next_sibling = self.children[1]
        return child


@pytest.fixture(autouse=True)
def _fake_indicator(monkeypatch):
    monkeypatch.setattr(sidebar_module, "DragIndicator", _Indicator)


def _row(box):
    return types.SimpleNamespace(
        drop_indicator_top=None,
        drop_indicator_bottom=None,
        get_child=lambda: box,
    )


def test_no_indicator_exists_until_one_is_shown():
    content = object()
    box = _Box(content)
    row = _row(box)

    sidebar_module._hide_row_drop_indicators(row)

    assert box.children == [content]
    assert row.drop_indicator_top is None
    assert row.drop_indicator_bottom is None


def test_top_goes_first_and_bottom_goes_last():
    content, extra = object(), object()
    box = _Box(content, extra)
    row = _row(box)

    sidebar_module._show_row_drop_indicator(row, True)
    sidebar_module._show_row_drop_indicator(row, False)

    assert box.children == [row.drop_indicator_top, content, extra,
                            row.drop_indicator_bottom]
    assert row.drop_indicator_top.visible
    assert row.drop_indicator_bottom.visible


def test_repeated_drags_reuse_the_same_indicator():
    box = _Box(object())
    row = _row(box)

    sidebar_module._show_row_drop_indicator(row, True)
    first = row.drop_indicator_top
    sidebar_module._hide_row_drop_indicators(row)
    assert not first.visible

    sidebar_module._show_row_drop_indicator(row, True)

    assert row.drop_indicator_top is first
    assert first.visible
    assert box.children.count(first) == 1


def test_row_without_a_box_shows_nothing():
    row = _row(None)

    sidebar_module._show_row_drop_indicator(row, False)

    assert row.drop_indicator_bottom is None


def test_set_indentation_skips_a_top_indicator_to_find_the_content():
    content = types.SimpleNamespace(get_margin_start=lambda: 12)
    box = _Box(content)
    row = types.SimpleNamespace(
        drop_indicator_top=None,
        drop_indicator_bottom=None,
        _content_box=None,
        _content_margin_base=None,
        _row_margin_base=None,
        get_child=lambda: box,
        get_margin_start=lambda: 0,
    )
    applied = []
    row._apply_indentation = lambda: applied.append(row._content_box)

    sidebar_module._show_row_drop_indicator(row, True)
    sidebar_module.ConnectionRow.set_indentation(row, 1)

    assert box.children[0] is row.drop_indicator_top
    assert row._content_box is content
    assert applied == [content]
