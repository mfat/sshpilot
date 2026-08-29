"""parse_ssh_config_outline extracts Host/Match headers (with line indices) for
the SSH config editor's navigation sidebar."""

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from sshpilot.text_editor import (
    collapse_selection_if_click_inside,
    parse_ssh_config_outline,
)


SAMPLE = """\
# my ssh config
Host web db
    HostName web.example
    Port 22

Host prod
    HostName prod.example

# Host commented-out
Match host *.internal
    ForwardAgent yes
"""


def test_extracts_hosts_and_match_with_line_indices():
    out = parse_ssh_config_outline(SAMPLE)
    assert out == [
        (1, "host", "web db"),
        (5, "host", "prod"),
        (9, "match", "host *.internal"),
    ]


def test_ignores_comments_and_value_keywords():
    # '# Host ...' is a comment; 'HostName' must not match as a Host header.
    out = parse_ssh_config_outline("# Host x\nHostName y\n    HostName z\n")
    assert out == []


def test_indented_host_is_matched():
    out = parse_ssh_config_outline("\tHost tabbed\n")
    assert out == [(0, "host", "tabbed")]


def test_host_without_pattern_falls_back_to_keyword():
    out = parse_ssh_config_outline("Host\n")
    assert out == [(0, "host", "Host")]


def test_empty_text():
    assert parse_ssh_config_outline("") == []
    assert parse_ssh_config_outline(None) == []


class _Iter:
    def __init__(self, offset: int) -> None:
        self.offset = offset

    def in_range(self, start: "_Iter", end: "_Iter") -> bool:
        return start.offset <= self.offset < end.offset

    def equal(self, other: "_Iter") -> bool:
        return self.offset == other.offset


class _Buffer:
    def __init__(self, start: int, end: int) -> None:
        self._sel = (_Iter(start), _Iter(end))
        self.cursor = None

    def get_selection_bounds(self):
        if self._sel is None:
            raise ValueError
        return self._sel

    def place_cursor(self, it: _Iter) -> None:
        self.cursor = it
        self._sel = None


def test_click_inside_selection_collapses_for_new_drag():
    """GtkTextView starts DND when a drag begins inside a selection (GH #1215)."""
    buf = _Buffer(0, 4)
    assert collapse_selection_if_click_inside(buf, _Iter(2)) is True
    assert buf.cursor.offset == 2
    assert buf._sel is None


def test_click_outside_selection_leaves_it():
    buf = _Buffer(0, 3)
    assert collapse_selection_if_click_inside(buf, _Iter(5)) is False
    assert buf._sel[0].offset == 0
    assert buf._sel[1].offset == 3
    assert buf.cursor is None
