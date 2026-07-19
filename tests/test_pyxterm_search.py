"""PyXterm SearchAddon integration (term/options, no double-find, decorations)."""
import json

from sshpilot.terminal_backends import PyXtermTerminalBackend


def _make_backend():
    b = object.__new__(PyXtermTerminalBackend)
    b.available = True
    b._webview = object()
    b._current_search_term = None
    b._current_search_is_regex = False
    b._current_search_case_sensitive = False
    b._search_addon_loaded = False
    b._SEARCH_DECORATIONS = dict(PyXtermTerminalBackend._SEARCH_DECORATIONS)
    b._scripts = []
    b._run_javascript = b._scripts.append
    return b


def test_search_set_query_does_not_find():
    b = _make_backend()
    b.search_set_query("foo.bar", case_sensitive=False, regex=False)
    assert b._current_search_term == "foo.bar"
    assert b._current_search_is_regex is False
    assert b._current_search_case_sensitive is False
    assert b._scripts == []  # no findNext on set


def test_search_set_query_keeps_literal_special_chars():
    b = _make_backend()
    b.search_set_query("a+b*", case_sensitive=False, regex=False)
    b.search_find_next()
    assert len(b._scripts) == 1
    assert "sshpilotSearch" in b._scripts[0]
    # Payload is JSON — term is the raw literal, not re.escaped.
    start = b._scripts[0].index("(") + 1
    end = b._scripts[0].rindex(")")
    payload = json.loads(b._scripts[0][start:end])
    assert payload["term"] == "a+b*"
    assert payload["opts"]["regex"] is False
    assert payload["opts"]["caseSensitive"] is False
    assert payload["forward"] is True
    assert "decorations" in payload["opts"]
    assert payload["opts"]["decorations"]["matchOverviewRuler"]


def test_search_find_previous_sets_forward_false():
    b = _make_backend()
    b.search_set_query("x", case_sensitive=True, regex=True)
    b.search_find_previous()
    start = b._scripts[0].index("(") + 1
    end = b._scripts[0].rindex(")")
    payload = json.loads(b._scripts[0][start:end])
    assert payload["forward"] is False
    assert payload["opts"]["regex"] is True
    assert payload["opts"]["caseSensitive"] is True
    assert "incremental" not in payload["opts"]


def test_search_find_next_sets_incremental():
    b = _make_backend()
    b.search_set_query("x")
    b.search_find_next()
    start = b._scripts[0].index("(") + 1
    end = b._scripts[0].rindex(")")
    payload = json.loads(b._scripts[0][start:end])
    assert payload["forward"] is True
    assert payload["opts"]["incremental"] is True


def test_clear_query_clears_decorations():
    b = _make_backend()
    b.search_set_query("x")
    b._scripts.clear()
    b.search_set_query(None)
    assert b._current_search_term is None
    assert any("clearDecorations" in s for s in b._scripts)


def test_handle_search_result_error_state():
    from sshpilot.terminal_search import TerminalSearch

    s = object.__new__(TerminalSearch)
    s._search_has_match = False
    s.search_entry = type("E", (), {"get_text": lambda self: "needle"})()
    states = []
    s._set_search_error_state = lambda err: states.append(err)
    s._update_search_count_label = lambda i, c: None

    s.handle_search_result(False)
    assert states == [True]
    assert s._search_has_match is False

    s.handle_search_result(True, result_index=0, result_count=3)
    assert states[-1] is False
    assert s._search_has_match is True
