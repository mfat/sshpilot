"""Cheap, display-free checks on the accessibility metadata we publish.

Two kinds of check live here, neither of which needs a graphical session (the
Dogtail suite in ``tests/e2e/`` covers the running app):

1. the helpers in ``sshpilot.accessibility`` call GTK's API the way GTK
   actually expects — including the tristate quirk that silently degrades
   ``EXPANDED`` to ``EXPANDABLE`` when the value is not a plain int;
2. no icon-only control is given an accessible name containing a keyboard
   shortcut. Shortcuts are user-rebindable, so a name built from one is not a
   stable identifier for assistive tech or automation.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from sshpilot import accessibility

SRC = Path(__file__).resolve().parents[1] / "src" / "sshpilot"


# --- helper behaviour -------------------------------------------------------


class FakeGtk:
    """Just enough of the Gtk namespace for the helpers to address."""

    class AccessibleProperty:
        LABEL = "prop:label"
        DESCRIPTION = "prop:description"

    class AccessibleState:
        EXPANDED = "state:expanded"
        SELECTED = "state:selected"


class RecordingWidget:
    """Stands in for a GtkWidget and records the a11y calls made on it."""

    def __init__(self):
        self.properties = []
        self.resets = []
        self.states = []
        self.state_resets = []
        self.tooltip = None

    def update_property(self, properties, values):
        self.properties.append((list(properties), list(values)))

    def reset_property(self, prop):
        self.resets.append(prop)

    def update_state(self, states, values):
        self.states.append((list(states), list(values)))

    def reset_state(self, state):
        self.state_resets.append(state)

    def set_tooltip_text(self, text):
        self.tooltip = text


@pytest.fixture
def gtk(monkeypatch):
    monkeypatch.setattr(accessibility, "_gtk", lambda: FakeGtk)
    return FakeGtk


def test_set_accessible_name_updates_the_label_property(gtk):
    widget = RecordingWidget()
    assert accessibility.set_accessible_name(widget, "New Connection") is True
    assert widget.properties == [(["prop:label"], ["New Connection"])]


def test_empty_accessible_name_resets_rather_than_blanking(gtk):
    widget = RecordingWidget()
    accessibility.set_accessible_name(widget, "")
    assert widget.properties == []
    assert widget.resets == ["prop:label"]


def test_set_accessible_description_updates_the_description_property(gtk):
    widget = RecordingWidget()
    accessibility.set_accessible_description(widget, "root@192.0.2.10")
    assert widget.properties == [(["prop:description"], ["root@192.0.2.10"])]


def test_expanded_state_is_sent_as_a_plain_int(gtk):
    """Regression guard for a silent GTK/PyGObject failure mode.

    ``GTK_ACCESSIBLE_STATE_EXPANDED`` is a tristate read with
    ``g_value_get_int``. Passing ``True`` trips a GValue assertion and passing
    ``Gtk.AccessibleTristate.TRUE`` publishes ``EXPANDABLE`` *without*
    ``EXPANDED`` — no error, just a state assistive tech never sees.
    """
    widget = RecordingWidget()
    accessibility.set_accessible_expanded(widget, True)
    accessibility.set_accessible_expanded(widget, False)
    assert widget.states == [
        (["state:expanded"], [1]),
        (["state:expanded"], [0]),
    ]
    for _states, values in widget.states:
        assert all(type(value) is int for value in values)


def test_expanded_none_resets_the_state(gtk):
    widget = RecordingWidget()
    accessibility.set_accessible_expanded(widget, None)
    assert widget.state_resets == ["state:expanded"]


def test_selected_state_is_sent_as_a_plain_int(gtk):
    """``SELECTED`` is a tristate too, so the same int rule applies to it."""
    widget = RecordingWidget()
    accessibility.set_accessible_selected(widget, True)
    accessibility.set_accessible_selected(widget, False)
    assert widget.states == [
        (["state:selected"], [1]),
        (["state:selected"], [0]),
    ]
    for _states, values in widget.states:
        assert all(type(value) is int for value in values)


def test_selected_none_resets_the_state(gtk):
    widget = RecordingWidget()
    accessibility.set_accessible_selected(widget, None)
    assert widget.state_resets == ["state:selected"]


def test_label_icon_button_separates_the_name_from_the_tooltip(gtk):
    button = RecordingWidget()
    accessibility.label_icon_button(
        button, "New Connection", tooltip="New Connection (Ctrl+Shift+N)"
    )
    assert button.tooltip == "New Connection (Ctrl+Shift+N)"
    assert button.properties == [(["prop:label"], ["New Connection"])]


def test_label_icon_button_uses_the_name_as_tooltip_when_none_is_given(gtk):
    """It must stay a drop-in replacement for ``set_tooltip_text(name)``.

    Regression guard: an earlier version only set the tooltip when one was
    passed explicitly, which silently removed the tooltip from nine icon-only
    controls that had one before.
    """
    button = RecordingWidget()
    accessibility.label_icon_button(button, "New Group")
    assert button.tooltip == "New Group"
    assert button.properties == [(["prop:label"], ["New Group"])]


def test_helpers_are_inert_without_gtk():
    """The unit suite runs against a stubbed ``gi``; nothing may explode."""
    assert accessibility.set_accessible_name(object(), "x") is False
    assert accessibility.set_accessible_expanded(object(), True) is False
    assert accessibility.set_accessible_selected(object(), True) is False
    accessibility.label_icon_button(None, "x")  # must not raise


# --- names must not embed keyboard shortcuts --------------------------------

#: Anything that means "a keyboard accelerator is being interpolated here".
_SHORTCUT_MARKERS = re.compile(
    r"Ctrl\+|Cmd\+|Alt\+|Shift\+|\{shortcut\}|\{modifier\}|\{accels\}|\{accel\}"
)


def _literal_strings(node: ast.AST) -> list:
    """Every string literal reachable in an expression (including f-string and
    ``.format()`` templates), so a name assembled from parts is still checked."""
    return [
        child.value
        for child in ast.walk(node)
        if isinstance(child, ast.Constant) and isinstance(child.value, str)
    ]


def _accessible_name_expressions():
    """Yield ``(module, lineno, expression)`` for every accessible name we set.

    The scan covers the whole package rather than the handful of modules that
    happened to need names first: a name added anywhere — a dialog, a command
    block, a key manager — is bound by the same rules, and a guard that only
    watched four files would have let the next one through.
    """
    for path in sorted(SRC.rglob("*.py")):
        module = str(path.relative_to(SRC))
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
            if name == "label_icon_button" and len(node.args) >= 2:
                yield module, node.lineno, node.args[1]
            elif name == "set_accessible_name" and len(node.args) >= 2:
                yield module, node.lineno, node.args[1]


def test_accessible_names_never_embed_a_keyboard_shortcut():
    checked = 0
    offenders = []
    for module, lineno, expression in _accessible_name_expressions():
        checked += 1
        for literal in _literal_strings(expression):
            if _SHORTCUT_MARKERS.search(literal):
                offenders.append(f"{module}:{lineno}: {literal!r}")
    assert checked > 10, "the scan found no accessible names — did the API change?"
    assert not offenders, (
        "accessible names must stay stable when a user rebinds a shortcut; "
        "put the accelerator in the tooltip instead:\n" + "\n".join(offenders)
    )


def test_accessible_names_are_not_internal_identifiers():
    """No widget attribute names, class names, or ids leak into a11y names."""
    bad = re.compile(r"^(?:[a-z_]+_(?:button|row|entry|widget|box)|Gtk[A-Z]|[0-9a-f]{8}-)")
    offenders = []
    for module, lineno, expression in _accessible_name_expressions():
        for literal in _literal_strings(expression):
            if bad.match(literal):
                offenders.append(f"{module}:{lineno}: {literal!r}")
    assert not offenders, "\n".join(offenders)
