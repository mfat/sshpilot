"""The startup sidebar build: visible rows first, the rest after the frame.

``rebuild_connection_list(first_rows=N)`` builds N visible connection rows and
leaves the rest to idle callbacks; every row is added with the visibility it
ends up with, so the list can be painted at any point of the build.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import sshpilot.window as window_module
from sshpilot.sidebar import GroupRow
from sshpilot.window import MainWindow


class _Idle:
    """Stand-in for GLib.idle_add/source_remove that runs callbacks on demand."""

    def __init__(self, monkeypatch):
        self.sources = {}
        self._next = 1
        monkeypatch.setattr(window_module.GLib, "idle_add", self.add)
        monkeypatch.setattr(window_module.GLib, "source_remove", self.remove)

    def add(self, callback, *args, **_kwargs):
        source = self._next
        self._next += 1
        self.sources[source] = (callback, args)
        return source

    def remove(self, source):
        self.sources.pop(source, None)

    def run(self):
        while self.sources:
            source = min(self.sources)
            callback, args = self.sources[source]
            if callback(*args) != window_module.GLib.SOURCE_CONTINUE:
                self.sources.pop(source, None)


def _window(rows):
    """A window whose rebuild yields one step per entry of ``rows``."""
    window = MainWindow.__new__(MainWindow)
    window.built = []
    window.finished = False
    window._apply_sidebar_row_actions = lambda force=False: None

    def steps():
        for name, visible in rows:
            window.built.append(name)
            yield visible
        window.finished = True

    window._rebuild_connection_list_steps = steps
    return window


def test_without_first_rows_the_rebuild_is_synchronous(monkeypatch):
    idle = _Idle(monkeypatch)
    window = _window([("a", True), ("b", True), ("c", True)])

    window.rebuild_connection_list()

    assert window.built == ["a", "b", "c"] and window.finished
    assert idle.sources == {}


def test_first_rows_counts_visible_rows_and_defers_the_rest(monkeypatch):
    idle = _Idle(monkeypatch)
    window = _window([("a", True), ("hidden", False), ("b", True), ("c", True)])

    window.rebuild_connection_list(first_rows=2)

    assert window.built == ["a", "hidden", "b"]
    assert not window.finished
    idle.run()
    assert window.built == ["a", "hidden", "b", "c"] and window.finished
    assert window._pending_connection_rows is None


def test_a_short_list_finishes_at_once(monkeypatch):
    idle = _Idle(monkeypatch)
    window = _window([("a", True)])

    window.rebuild_connection_list(first_rows=5)

    assert window.finished
    assert idle.sources == {}


def test_a_later_rebuild_drops_the_pending_rows(monkeypatch):
    idle = _Idle(monkeypatch)
    window = _window([("a", True), ("b", True), ("c", True)])
    window.rebuild_connection_list(first_rows=1)
    first_build = window._pending_connection_rows

    window._rebuild_connection_list_steps = lambda: iter(())
    window.rebuild_connection_list()
    idle.run()

    assert window.built == ["a"]
    assert window._pending_connection_rows is None
    assert first_build.gi_frame is None  # closed


def test_complete_pending_rows_builds_them_now(monkeypatch):
    idle = _Idle(monkeypatch)
    window = _window([("a", True), ("b", True), ("c", True)])
    window.rebuild_connection_list(first_rows=1)

    window.complete_pending_connection_rows()

    assert window.built == ["a", "b", "c"] and window.finished
    assert idle.sources == {}
    window.complete_pending_connection_rows()  # nothing pending: no-op


class _Row:
    def __init__(self, name):
        self.name = name
        self.visible = True

    def set_visible(self, visible):
        self.visible = bool(visible)


def test_grouped_rows_get_their_final_visibility_as_they_are_added(monkeypatch):
    class _FakeGroupRow:
        apply_descendant_visibility = GroupRow.apply_descendant_visibility

        def __init__(self, group_info, group_manager, connections_dict=None):
            self.group_info = group_info
            self.name = group_info["id"]
            self._member_rows = []
            self._child_group_rows = []
            self.visible = True

        def connect(self, *args, **kwargs):
            pass

        def set_indentation(self, level):
            pass

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
    window.connection_list_append = None
    added = []

    def add_connection_row(conn, level, display_group_id=None):
        row = _Row(conn)
        added.append(row)
        return row

    window.add_connection_row = add_connection_row
    hierarchy = [
        {"id": "open", "expanded": True, "connections": ["c1"], "children": [
            {"id": "shut", "expanded": False, "connections": ["c2"], "children": []},
        ]},
    ]
    steps = window._build_grouped_list_steps(
        hierarchy, {"c1": "c1", "c2": "c2"}, 0, True
    )

    # Paused after each row: what is already in the list must not change.
    assert next(steps) is True
    assert [(row.name, row.visible) for row in added] == [("c1", True)]
    assert next(steps) is False
    assert [(row.name, row.visible) for row in added] == [("c1", True), ("c2", False)]


def test_rows_under_a_collapsed_section_are_added_hidden(monkeypatch):
    class _FakeGroupRow:
        apply_descendant_visibility = GroupRow.apply_descendant_visibility

        def __init__(self, group_info, *_args):
            self.group_info = group_info
            self._member_rows = []
            self._child_group_rows = []

        connect = set_indentation = lambda self, *a, **k: None

        def add_member_row(self, row):
            self._member_rows.append(row)

        def set_visible(self, visible):
            self.visible = bool(visible)

    monkeypatch.setattr(window_module, "GroupRow", _FakeGroupRow)
    window = MainWindow.__new__(MainWindow)
    window.group_manager = object()
    window.connection_list = []
    window.add_connection_row = lambda conn, level, display_group_id=None: _Row(conn)
    hierarchy = [{"id": "g", "expanded": True, "connections": ["c1"], "children": []}]

    steps = window._build_grouped_list_steps(hierarchy, {"c1": "c1"}, 0, False)

    assert next(steps) is False


def test_parsing_the_command_line_does_not_load_the_connection_stack():
    """The daemon is spawned right after the pre-parse; keep it light."""
    src = Path(__file__).resolve().parents[1] / "src"
    code = (
        "import sys\n"
        "from sshpilot.cli_connect import parse_sshpilot_cli\n"
        "parse_sshpilot_cli(['--verbose'])\n"
        "heavy = {'asyncio', 'sshpilot.connection_model',"
        " 'sshpilot.ssh_connection_builder', 'gi'} & set(sys.modules)\n"
        "print(sorted(heavy))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=str(src),
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "[]"
