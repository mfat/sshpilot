"""Selection semantics match the reference VTE terminals.

Three behaviors that used to diverge from GNOME Terminal / Ptyxis:

* word-wise selection kept VTE's default exception set instead of a narrower
  one (VTE silently drops a ``-`` that is not the first character, so hyphenated
  tokens broke apart);
* selection colors stay unset so VTE draws selections reversed and colored
  output survives being selected;
* copy-on-select acts only on settled, user-driven selections.
"""

import types

import pytest

pytest.importorskip("gi")

from sshpilot.terminal import TerminalWidget
from sshpilot.terminal_backends import (
    PyXtermBridgeBackend,
    PyXtermTerminalBackend,
    VTETerminalBackend,
    _translucent_css,
)


# -- word-wise selection -------------------------------------------------------


def test_configure_never_narrows_vte_word_char_exceptions():
    """VTE's default set already covers ``-``; forcing a subset broke it.

    ``Terminal::process_word_char_exceptions`` skips a ``-`` that is not the
    first character of the string, so any list that mentions the hyphen later
    loses it outright and double-clicking ``my-branch-name`` selects a fragment.
    """
    calls = []
    backend = object.__new__(VTETerminalBackend)
    backend.vte = types.SimpleNamespace(
        set_word_char_exceptions=lambda value: calls.append(value),
        set_word_char_options=lambda value: calls.append(value),
    )

    backend.configure({})

    for value in calls:
        assert value.startswith("-"), f"VTE would drop the hyphen from {value!r}"


# -- selection colors ----------------------------------------------------------


def test_reset_highlight_unsets_both_vte_selection_colors():
    calls = {}
    backend = object.__new__(VTETerminalBackend)
    backend.vte = types.SimpleNamespace(
        set_color_highlight=lambda color: calls.update(bg=color),
        set_color_highlight_foreground=lambda color: calls.update(fg=color),
    )

    backend._reset_highlight()

    # Only an unset pair makes VTE fall back to reverse video, which is what
    # keeps red errors / ls colors / diffs colored while selected.
    assert calls == {"bg": None, "fg": None}
    assert backend._selection_background is None
    assert backend._selection_foreground is None


def test_leaving_search_highlight_returns_to_reverse_video():
    calls = []
    backend = object.__new__(VTETerminalBackend)
    backend.vte = types.SimpleNamespace(
        set_color_highlight=lambda color: calls.append(("bg", color)),
        set_color_highlight_foreground=lambda color: calls.append(("fg", color)),
    )

    backend.set_search_highlight(False)

    assert calls == [("bg", None), ("fg", None)]


def test_pyxterm_theme_tints_selection_without_forcing_a_foreground():
    backend = object.__new__(PyXtermTerminalBackend)
    backend.available = True
    backend.owner = types.SimpleNamespace(
        config=types.SimpleNamespace(
            get_setting=lambda key, default=None: "default",
            get_terminal_profile=lambda _name: {
                "background": "#112233",
                "foreground": "#abcdef",
                "cursor_color": "#ffffff",
                "highlight_background": "#4A90E2",
                "highlight_foreground": "#ffffff",
                "palette": [],
            },
        )
    )
    scripts = []
    backend._run_javascript = scripts.append

    backend.apply_theme("default")

    js = scripts[0]
    # xterm.js keeps each cell's own color only when selectionForeground is
    # absent -- the closest it gets to VTE's reverse-video selection.
    assert "selectionForeground" not in js
    assert _translucent_css("#abcdef", 0.35) in js
    assert "#4A90E2" not in js


# -- copy-on-select ------------------------------------------------------------


def test_vte_search_navigation_marks_the_selection_uncopyable():
    """VTE's search selects the hit and emits selection-changed inline."""
    seen = []
    backend = object.__new__(VTETerminalBackend)
    backend._selection_from_search = False
    backend.vte = types.SimpleNamespace(
        search_find_next=lambda: seen.append(backend.selection_change_is_copyable()),
        search_find_previous=lambda: seen.append(
            backend.selection_change_is_copyable()
        ),
    )

    assert backend.selection_change_is_copyable() is True
    backend.search_find_next()
    backend.search_find_previous()

    assert seen == [False, False]
    assert backend.selection_change_is_copyable() is True


@pytest.mark.parametrize(
    "payload, copyable",
    [
        ({"hasSelection": True}, True),
        ({"hasSelection": True, "dragging": True}, False),
        ({"hasSelection": True, "fromSearch": True}, False),
    ],
)
def test_pyxterm_reports_drag_and_search_selections_as_uncopyable(payload, copyable):
    import json

    # The message pump lives on the bridge subclass; the flags and the gate it
    # feeds are inherited from PyXtermTerminalBackend.
    backend = object.__new__(PyXtermBridgeBackend)
    backend._has_selection = False
    backend._selection_dragging = False
    backend._selection_from_search = False
    backend._shortcut_passthrough = False
    backend._selection_changed_cb = None
    message = dict(payload, type="selection-changed")

    backend._on_pty_message(
        None, types.SimpleNamespace(to_json=lambda _indent: json.dumps(message))
    )

    assert backend.selection_change_is_copyable() is copyable


def test_copy_on_select_skips_uncopyable_selection_changes():
    copies = []
    backend = types.SimpleNamespace(
        get_has_selection=lambda: True,
        copy_clipboard=lambda: copies.append(True),
        selection_change_is_copyable=lambda: False,
    )
    widget = types.SimpleNamespace(
        backend=backend,
        config=types.SimpleNamespace(get_setting=lambda _key, _default: True),
    )

    TerminalWidget._on_selection_changed(widget)

    assert copies == []


def test_copy_on_select_still_fires_for_settled_user_selections():
    copies = []
    backend = types.SimpleNamespace(
        get_has_selection=lambda: True,
        copy_clipboard=lambda: copies.append(True),
        selection_change_is_copyable=lambda: True,
    )
    widget = types.SimpleNamespace(
        backend=backend,
        config=types.SimpleNamespace(get_setting=lambda _key, _default: True),
    )

    TerminalWidget._on_selection_changed(widget)

    assert copies == [True]
