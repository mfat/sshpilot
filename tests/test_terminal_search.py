"""Characterization tests for TerminalWidget search — pin current behavior before
it is extracted into a composed TerminalSearch object. Both backends (sync VTE,
async PyXterm) and the documented edge cases are covered.

Uses the suite's standard headless pattern: build a bare TerminalWidget via
``__new__`` and stub only the attributes each method touches.
"""
import pytest

pytest.importorskip("gi")

from sshpilot.terminal import TerminalWidget


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


def _tw(**attrs):
    tw = TerminalWidget.__new__(TerminalWidget)
    tw._last_search_text = ""
    tw._last_search_case_sensitive = False
    tw._last_search_regex = False
    tw._search_has_match = False
    tw.backend = None
    tw.search_entry = _Entry()
    tw.search_count_label = _Label()
    tw.search_prev_button = _Button()
    tw.search_next_button = _Button()
    for k, v in attrs.items():
        setattr(tw, k, v)
    return tw


# --- _update_search_count_label ------------------------------------------------

def test_count_label_empty_when_no_results():
    tw = _tw()
    tw._update_search_count_label(-1, 0)
    assert tw.search_count_label.text == ""


def test_count_label_threshold_plus_when_index_negative():
    tw = _tw()
    tw._update_search_count_label(-1, 42)
    assert tw.search_count_label.text == "42+"


def test_count_label_index_over_count():
    tw = _tw()
    tw._update_search_count_label(2, 5)
    assert tw.search_count_label.text == "3/5"


# --- _run_search (sync VTE) ----------------------------------------------------

def test_run_search_vte_found_sets_match_no_error():
    tw = _tw(backend=_VteBackend(found=True))
    assert tw._run_search(True, update_entry=True, from_text_change=True) is True
    assert tw._search_has_match is True
    assert "error" not in tw.search_entry.classes


def test_run_search_vte_not_found_from_text_change_sets_error():
    tw = _tw(backend=_VteBackend(found=False))
    assert tw._run_search(True, update_entry=True, from_text_change=True) is False
    assert "error" in tw.search_entry.classes


def test_run_search_vte_wraparound_single_match_does_not_error():
    # Navigating (not from_text_change) with an existing match: VTE returns False
    # for the single already-highlighted match, but the entry must NOT go red.
    tw = _tw(backend=_VteBackend(found=False), _search_has_match=True)
    assert tw._run_search(True, update_entry=True, from_text_change=False) is False
    assert "error" not in tw.search_entry.classes


def test_run_search_pyxterm_returns_true_and_skips_sync_error():
    tw = _tw(backend=PyXtermTerminalBackend())
    # even with update_entry, the async backend must not set error state synchronously
    assert tw._run_search(True, update_entry=True, from_text_change=True) is True
    assert "error" not in tw.search_entry.classes


# --- _update_search_pattern ----------------------------------------------------

def test_update_pattern_empty_text_clears():
    backend = _VteBackend()
    tw = _tw(backend=backend)
    tw._last_search_text = "old"
    assert tw._update_search_pattern("") is False
    assert tw._last_search_text == ""
    assert tw.search_prev_button.sensitive is False


def test_update_pattern_query_backend_passes_raw_term_and_flags():
    backend = _VteBackend()
    tw = _tw(backend=backend)
    tw._update_search_pattern("a+b*", case_sensitive=True, regex=False, move_forward=False)
    assert backend.queries == [("a+b*", True, False)]  # raw, not re.escaped
    assert tw.search_next_button.sensitive is True
    assert tw._last_search_text == "a+b*"


def test_update_pattern_backend_error_sets_error_state():
    class _Boom(_VteBackend):
        def search_set_query(self, *a, **k):
            raise RuntimeError("compile failed")

    tw = _tw(backend=_Boom())
    assert tw._update_search_pattern("x", update_entry=True, move_forward=False) is False
    assert "error" in tw.search_entry.classes


# --- PyXterm async callbacks ---------------------------------------------------

def test_handle_search_results_decoration_counts():
    tw = _tw(search_entry=_Entry("needle"))
    tw.handle_search_results(1, 4)
    assert tw._search_has_match is True
    assert "error" not in tw.search_entry.classes
    assert tw.search_count_label.text == "2/4"

    tw.handle_search_results(-1, 0)
    assert tw._search_has_match is False
    assert "error" in tw.search_entry.classes
    assert tw.search_count_label.text == ""


# --- _clear_search_pattern -----------------------------------------------------

def test_clear_search_pattern_resets_and_queries_none():
    backend = _VteBackend()
    tw = _tw(backend=backend, _search_has_match=True)
    tw._last_search_text = "abc"
    tw.search_entry.add_css_class("error")
    tw._clear_search_pattern()
    assert tw._last_search_text == ""
    assert tw._search_has_match is False
    assert tw.search_prev_button.sensitive is False
    assert "error" not in tw.search_entry.classes
    assert tw.search_count_label.text == ""
    assert backend.queries == [(None, False, False)]


# --- _hide_search_overlay ------------------------------------------------------

def test_hide_search_overlay_resets_error_and_count():
    calls = {"cursor": 0, "decorations": 0, "focus": 0}

    class _Revealer:
        def __init__(self):
            self.revealed = True

        def set_reveal_child(self, v):
            self.revealed = v

    class _Backend:
        def clear_search_decorations(self):
            calls["decorations"] += 1

        def grab_focus(self):
            calls["focus"] += 1

    tw = _tw(backend=_Backend(), search_revealer=_Revealer())
    tw.search_entry.add_css_class("error")
    tw._apply_cursor_and_selection_colors = lambda: calls.__setitem__("cursor", calls["cursor"] + 1)

    tw._hide_search_overlay()
    assert tw.search_revealer.revealed is False
    assert "error" not in tw.search_entry.classes
    assert tw.search_count_label.text == ""
    assert calls == {"cursor": 1, "decorations": 1, "focus": 1}
