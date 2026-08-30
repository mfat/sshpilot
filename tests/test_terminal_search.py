"""Characterization tests for the terminal search feature (sshpilot.terminal_search).

Pins current behavior of the search block after its extraction into a composed
``TerminalSearch`` object: sync VTE path, async PyXterm callbacks, count-label
branches, the wrap-around single-match no-error edge, backend-error styling, and
clear/hide reset. Built via ``__new__`` with a stubbed terminal back-reference
(``s.t``) and stubbed widgets.
"""
from types import SimpleNamespace
from pathlib import Path

import pytest

pytest.importorskip("gi")

from sshpilot.terminal_search import TerminalSearch


class _Entry:
    def __init__(self, text=""):
        self._text = text
        self.classes = set()

    def get_text(self):
        return self._text

    def add_css_class(self, c):
        self.classes.add(c)

    def remove_css_class(self, c):
        self.classes.discard(c)


class _Label:
    def __init__(self):
        self.text = None

    def set_text(self, t):
        self.text = t


class _Button:
    def __init__(self):
        self.sensitive = None

    def set_sensitive(self, v):
        self.sensitive = v


class _VteBackend:
    """Sync backend: search_find_* returns a real bool. Name is not PyXterm."""

    def __init__(self, found=False):
        self._found = found
        self.queries = []

    def search_find_next(self):
        return self._found

    def search_find_previous(self):
        return self._found

    def search_set_query(self, text, *, case_sensitive=False, regex=False):
        self.queries.append((text, case_sensitive, regex))


class PyXtermTerminalBackend:
    """Name matches the real class so _is_pyxterm_backend() is True."""

    def search_find_next(self):
        return True

    def search_find_previous(self):
        return True


def _search(**attrs):
    s = TerminalSearch.__new__(TerminalSearch)
    s.t = SimpleNamespace(backend=None, grab_terminal_focus=lambda: None)
    s._last_search_text = ""
    s._last_search_case_sensitive = False
    s._last_search_regex = False
    s._search_has_match = False
    s.search_entry = _Entry()
    s.search_count_label = _Label()
    s.search_prev_button = _Button()
    s.search_next_button = _Button()
    for k, v in attrs.items():
        if k == "backend":
            s.t.backend = v
        else:
            setattr(s, k, v)
    return s


# --- _update_search_count_label ------------------------------------------------

def test_count_label_empty_when_no_results():
    s = _search()
    s._update_search_count_label(-1, 0)
    assert s.search_count_label.text == ""


def test_count_label_threshold_plus_when_index_negative():
    s = _search()
    s._update_search_count_label(-1, 42)
    assert s.search_count_label.text == "42+"


def test_count_label_index_over_count():
    s = _search()
    s._update_search_count_label(2, 5)
    assert s.search_count_label.text == "3/5"


# --- _run_search (sync VTE) ----------------------------------------------------

def test_run_search_vte_found_sets_match_no_error():
    s = _search(backend=_VteBackend(found=True))
    assert s._run_search(True, update_entry=True, from_text_change=True) is True
    assert s._search_has_match is True
    assert "error" not in s.search_entry.classes


def test_run_search_vte_not_found_from_text_change_sets_error():
    s = _search(backend=_VteBackend(found=False))
    assert s._run_search(True, update_entry=True, from_text_change=True) is False
    assert "error" in s.search_entry.classes


def test_run_search_vte_wraparound_single_match_does_not_error():
    # Navigating (not from_text_change) with an existing match: VTE returns False
    # for the single already-highlighted match, but the entry must NOT go red.
    s = _search(backend=_VteBackend(found=False), _search_has_match=True)
    assert s._run_search(True, update_entry=True, from_text_change=False) is False
    assert "error" not in s.search_entry.classes


def test_run_search_pyxterm_returns_true_and_skips_sync_error():
    s = _search(backend=PyXtermTerminalBackend())
    assert s._run_search(True, update_entry=True, from_text_change=True) is True
    assert "error" not in s.search_entry.classes


# --- _update_search_pattern ----------------------------------------------------

def test_update_pattern_empty_text_clears():
    s = _search(backend=_VteBackend())
    s._last_search_text = "old"
    assert s._update_search_pattern("") is False
    assert s._last_search_text == ""
    assert s.search_prev_button.sensitive is False


def test_update_pattern_query_backend_passes_raw_term_and_flags():
    backend = _VteBackend()
    s = _search(backend=backend)
    s._update_search_pattern("a+b*", case_sensitive=True, regex=False, move_forward=False)
    assert backend.queries == [("a+b*", True, False)]  # raw, not re.escaped
    assert s.search_next_button.sensitive is True
    assert s._last_search_text == "a+b*"


def test_update_pattern_backend_error_sets_error_state():
    class _Boom(_VteBackend):
        def search_set_query(self, *a, **k):
            raise RuntimeError("compile failed")

    s = _search(backend=_Boom())
    assert s._update_search_pattern("x", update_entry=True, move_forward=False) is False
    assert "error" in s.search_entry.classes


# --- PyXterm async callbacks ---------------------------------------------------

def test_handle_search_results_decoration_counts():
    s = _search(search_entry=_Entry("needle"))
    s.handle_search_results(1, 4)
    assert s._search_has_match is True
    assert "error" not in s.search_entry.classes
    assert s.search_count_label.text == "2/4"

    s.handle_search_results(-1, 0)
    assert s._search_has_match is False
    assert "error" in s.search_entry.classes
    assert s.search_count_label.text == ""


# --- _clear_search_pattern -----------------------------------------------------

def test_clear_search_pattern_resets_and_queries_none():
    backend = _VteBackend()
    s = _search(backend=backend, _search_has_match=True)
    s._last_search_text = "abc"
    s.search_entry.add_css_class("error")
    s._clear_search_pattern()
    assert s._last_search_text == ""
    assert s._search_has_match is False
    assert s.search_prev_button.sensitive is False
    assert "error" not in s.search_entry.classes
    assert s.search_count_label.text == ""
    assert backend.queries == [(None, False, False)]


# --- _hide_search_overlay ------------------------------------------------------

def test_hide_search_overlay_resets_error_and_count():
    calls = {"decorations": 0, "focus": 0}

    class _Revealer:
        def __init__(self):
            self.revealed = True
            self.visible = True

        def set_reveal_child(self, v):
            self.revealed = v

        def set_visible(self, v):
            self.visible = v

    class _Backend:
        def clear_search_decorations(self):
            calls["decorations"] += 1

    s = _search(backend=_Backend(), search_revealer=_Revealer())
    s.t.grab_terminal_focus = lambda: calls.__setitem__("focus", calls["focus"] + 1)
    s.search_entry.add_css_class("error")

    s._hide_search_overlay()
    assert s.search_revealer.revealed is False
    assert s.search_revealer.visible is False
    assert "error" not in s.search_entry.classes
    assert s.search_count_label.text == ""
    assert calls == {"decorations": 1, "focus": 1}


def test_show_search_highlight_uses_backend_without_vte_escape_hatch():
    calls = []
    backend = SimpleNamespace(set_search_highlight=lambda active: calls.append(active))
    s = _search(backend=backend)

    s._apply_search_highlight_colors()

    assert calls == [True]
    assert not hasattr(s.t, "vte")


def test_search_layout_is_shared_and_keeps_backend_viewport_truthful():
    """The shared stack, not either emulator, owns search viewport layout."""
    root = Path(__file__).parents[1]
    terminal_source = (root / "src/sshpilot/terminal.py").read_text()
    search_source = (root / "src/sshpilot/terminal_search.py").read_text()

    assert "self.overlay.add_overlay(self.search_revealer)" not in terminal_source
    assert "Gtk.RevealerTransitionType.NONE" in search_source

    stack_start = terminal_source.index("self.terminal_stack = Gtk.Box")
    stack_end = terminal_source.index("# Container for terminal stack only", stack_start)
    stack_source = terminal_source[stack_start:stack_end]
    flow_children = (
        "self.search_revealer",
        "self.overlay",
        "self.disconnected_revealer",
        "self.save_connection_revealer",
    )
    positions = [stack_source.index(f"append({child})") for child in flow_children]
    assert positions == sorted(positions)
    assert "Vte." not in stack_source
    assert "PyXterm" not in stack_source


# --- _on_vte_search_key_pressed ------------------------------------------------
#
# This VTE-level controller once claimed Ctrl+F and Ctrl+G unconditionally, so
# unassigning "Search" in Preferences still did not hand Ctrl+F back to the shell
# and Ctrl+G never reached readline's abort at all. It must stay inert while the
# search bar is closed. Real GDK values are pinned here so the test means the same
# thing under the stubbed gi in conftest and under a real GTK.

_CONTROL = 1 << 2
_SHIFT = 1 << 0
_GDK = SimpleNamespace(
    KEY_f=0x066, KEY_F=0x046, KEY_g=0x067, KEY_G=0x047, KEY_Escape=0xFF1B,
    ModifierType=SimpleNamespace(CONTROL_MASK=_CONTROL, SHIFT_MASK=_SHIFT,
                                 META_MASK=1 << 28),
)


class _KeyRevealer:
    def __init__(self, revealed):
        self.revealed = revealed
        self.visible = revealed

    def get_reveal_child(self):
        return self.revealed

    def set_reveal_child(self, v):
        self.revealed = v

    def set_visible(self, v):
        self.visible = v


@pytest.fixture
def key_search(monkeypatch):
    from sshpilot import terminal_search as mod

    monkeypatch.setattr(mod, "Gdk", _GDK)

    def build(revealed):
        calls = {"next": 0, "prev": 0, "show": 0}
        s = _search(search_revealer=_KeyRevealer(revealed))
        s._on_search_next = lambda *a: calls.__setitem__("next", calls["next"] + 1)
        s._on_search_previous = lambda *a: calls.__setitem__("prev", calls["prev"] + 1)
        s._show_search_overlay = lambda **k: calls.__setitem__("show", calls["show"] + 1)
        s.t.grab_terminal_focus = lambda: None
        s.t.backend = None
        return s, calls

    return build


def _press(s, keyval, ctrl=False, shift=False):
    state = (_CONTROL if ctrl else 0) | (_SHIFT if shift else 0)
    return s._on_vte_search_key_pressed(None, keyval, 0, state)


@pytest.mark.parametrize("keyval, ctrl, shift", [
    (_GDK.KEY_f, True, False),       # readline forward-char
    (_GDK.KEY_F, True, True),        # opening search belongs to the registry action
    (_GDK.KEY_g, True, False),       # readline abort
    (_GDK.KEY_G, True, True),
    (_GDK.KEY_Escape, False, False),
])
def test_closed_search_bar_lets_every_key_reach_the_terminal(key_search, keyval, ctrl, shift):
    s, calls = key_search(revealed=False)
    assert _press(s, keyval, ctrl=ctrl, shift=shift) is False
    assert calls == {"next": 0, "prev": 0, "show": 0}


def test_open_search_bar_still_navigates_from_the_terminal(key_search):
    s, calls = key_search(revealed=True)
    assert _press(s, _GDK.KEY_g, ctrl=True) is True
    assert _press(s, _GDK.KEY_G, ctrl=True, shift=True) is True
    assert calls["next"] == 1 and calls["prev"] == 1


def test_open_search_bar_dismisses_on_escape(key_search):
    s, _calls = key_search(revealed=True)
    assert _press(s, _GDK.KEY_Escape) is True
    assert s.search_revealer.revealed is False


def test_ctrl_f_never_opens_search_from_the_terminal(key_search):
    """Ctrl+F is not a terminal-search binding; ``terminal-search`` owns opening."""
    for revealed in (False, True):
        s, calls = key_search(revealed=revealed)
        assert _press(s, _GDK.KEY_f, ctrl=True) is False
        assert calls["show"] == 0
