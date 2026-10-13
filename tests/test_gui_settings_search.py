"""GUI tests for searching Settings (its sidebar search, and omni-search).

The settings index (``sshpilot.settings_index``) is a hand-kept table so
omni-search can offer individual settings without building the window; the
first test fails when a page gains, renames or loses a row without the table
following.

Opt-in only: SSHPILOT_GUI_TESTS=1 pytest -m gui
"""

import pytest

from tests._gui_harness import requires_gui  # the `gui` fixture comes from conftest

requires_gui()

pytestmark = pytest.mark.gui


def _open_preferences(gui, page_id=None):
    gui.window.show_preferences(page_id)
    gui.pump(500)
    return gui.window._preferences_window


def _all_row_titles(page):
    """Every row title on *page*, hidden rows included (they show per backend)."""
    from gi.repository import Adw

    titles = set()
    stack = [(page, None)]
    while stack:
        widget, expander = stack.pop()
        if isinstance(widget, Adw.PreferencesGroup) and widget.get_title():
            # A group of custom controls is indexed by its title.
            if not _has_rows(widget):
                titles.add(widget.get_title())
                continue
        if isinstance(widget, Adw.PreferencesRow):
            title = widget.get_title()
            if title and not (expander is not None and title == expander.get_title()):
                titles.add(title)
            if isinstance(widget, Adw.ExpanderRow):
                expander = widget
        child = widget.get_first_child()
        while child is not None:
            stack.append((child, expander))
            child = child.get_next_sibling()
    return titles


def _has_rows(widget):
    from gi.repository import Adw

    child = widget.get_first_child()
    while child is not None:
        if isinstance(child, Adw.PreferencesRow) or _has_rows(child):
            return True
        child = child.get_next_sibling()
    return False


def _search(prefs, gui, text):
    prefs.start_search()
    prefs.search_entry.set_text(text)
    gui.pump(300)  # search-changed is debounced by GtkSearchEntry
    titles = []
    row = prefs.search_results.get_first_child()
    while row is not None:
        titles.append(row.get_title())
        row = row.get_next_sibling()
    return titles


def test_settings_index_matches_the_real_rows(gui):
    from sshpilot.settings_index import GENERATED_PAGES, NOT_INDEXED, PAGE_TITLES, SETTINGS

    prefs = _open_preferences(gui)
    for page_id in list(prefs._page_builders):
        if page_id not in GENERATED_PAGES:
            prefs._ensure_page_built(page_id)

    assert set(prefs.pages) == set(PAGE_TITLES), 'PAGE_TITLES is out of date'
    for page_id, page in prefs.pages.items():
        if page_id in GENERATED_PAGES:
            continue
        indexed = {e.title for e in SETTINGS if e.page_id == page_id}
        indexed |= {title for page, title in NOT_INDEXED if page == page_id}
        live = _all_row_titles(page)
        assert live - indexed == set(), f'{page_id}: rows missing from settings_index'
        assert indexed - live == set(), f'{page_id}: settings_index lists rows that are gone'


def test_search_finds_a_setting_and_reveals_it(gui):
    prefs = _open_preferences(gui, page_id='interface')
    titles = _search(prefs, gui, 'scroll lines')
    assert titles == ['Mouse wheel scroll lines']
    assert prefs.sidebar_stack.get_visible_child_name() == 'results'
    result = prefs.search_results.get_row_at_index(0)
    assert result.get_subtitle() == 'Terminal › Mouse'

    prefs.search_results.emit('row-activated', result)
    gui.pump(500)
    assert prefs._selected_page_name == 'terminal'
    target = result._search_target[1]
    focus = prefs.get_focus()
    # Focusing it is what scrolls the page to it (a SpinRow hands focus to its entry).
    assert focus is not None and (focus is target or focus.is_ancestor(target)), \
        'the revealed row was not focused (and so not scrolled to)'
    assert target.has_css_class('settings-search-highlight')
    # Results stay up, so the next match is one click away.
    assert prefs.search_bar.get_search_mode()


def test_search_uses_keywords_pages_and_skips_hidden_rows(gui):
    prefs = _open_preferences(gui)
    assert 'Application Theme' in _search(prefs, gui, 'dark mode')
    keepalive = _search(prefs, gui, 'keepalive')
    assert {'Apply default keepalive', 'ServerAlive Interval (s)'} <= set(keepalive)
    # A page whose name matches comes first.
    assert _search(prefs, gui, 'terminal')[0] == 'Terminal'

    # Rows hidden for the current backend are not offered.
    hidden = [row for _page, _group, row in prefs._iter_setting_rows()
              if row.get_title() == 'KeePass database (.kdbx)']
    assert hidden == []
    assert 'KeePass database (.kdbx)' not in _search(prefs, gui, 'kdbx')


def test_search_finds_groups_of_custom_controls(gui):
    """App Startup is radio buttons, not rows; it is still found and revealed."""
    from gi.repository import Gtk

    prefs = _open_preferences(gui, page_id='terminal')
    assert _search(prefs, gui, 'start page') == ['App Startup']
    result = prefs.search_results.get_row_at_index(0)
    assert result.get_subtitle() == 'Interface'
    prefs.search_results.emit('row-activated', result)
    gui.pump(500)
    group = result._search_target[1]
    assert prefs._selected_page_name == 'interface'
    focus = prefs.get_focus()
    assert isinstance(focus, Gtk.CheckButton) and focus.is_ancestor(group)
    assert group.has_css_class('settings-search-highlight')


def test_search_empty_state_and_escape(gui):
    prefs = _open_preferences(gui)
    assert _search(prefs, gui, 'zzzz-no-such-setting') == []
    assert prefs.sidebar_stack.get_visible_child_name() == 'empty'

    prefs.search_entry.set_text('')
    gui.pump(300)
    assert prefs.sidebar_stack.get_visible_child_name() == 'pages'

    _search(prefs, gui, 'font')
    prefs.search_entry.emit('stop-search')
    gui.pump(200)
    # Esc leaves search and keeps Settings open.
    assert not prefs.search_bar.get_search_mode()
    assert prefs.search_entry.get_text() == ''
    assert prefs.sidebar_stack.get_visible_child_name() == 'pages'
    assert prefs.get_visible()


def test_enter_opens_the_first_result(gui):
    from gi.repository import Adw

    prefs = _open_preferences(gui, page_id='interface')
    _search(prefs, gui, 'row style')
    prefs.search_entry.emit('activate')
    gui.pump(500)
    assert prefs._selected_page_name == 'sidebar'
    # Enter ends the search, as in GNOME Settings; the row still gets focus.
    assert not prefs.search_bar.get_search_mode()
    assert prefs.sidebar_stack.get_visible_child_name() == 'pages'
    focus = prefs.get_focus()
    assert focus is not None and focus.get_ancestor(Adw.ComboRow).get_title() == 'Row Style'


def test_search_does_not_build_the_plugins_page(gui):
    """Building Plugins downloads the plugin registry; searching must not."""
    from sshpilot.preferences import PreferencesWindow

    # A fresh instance: earlier tests may have opened Plugins on the shared one.
    window = gui.window
    prefs = PreferencesWindow(
        window, window.config,
        ssh_overrides_controller=window._build_ssh_overrides_controller())
    try:
        assert 'plugins' in prefs._page_builders
        titles = _search(prefs, gui, 'plugins')
        assert 'plugins' in prefs._page_builders
        assert titles[0] == 'Plugins'
    finally:
        prefs.destroy()


def test_ctrl_f_in_settings_searches_settings(gui):
    import types

    prefs = _open_preferences(gui)
    fake_app = types.SimpleNamespace(props=types.SimpleNamespace(active_window=prefs))
    type(gui.app).on_search(fake_app, None, None)
    gui.pump(200)
    assert prefs.search_bar.get_search_mode()
    assert prefs.get_visible(), 'Ctrl+F hid Settings'


def test_omni_search_offers_and_opens_a_single_setting(gui):
    from sshpilot.omni_search import search_omni

    results = search_omni(gui.window, 'copy on selection')
    setting = next(r for r in results if r.kind == 'setting')
    assert setting.title == 'Copy on selection'
    assert setting.subtitle == 'Settings › Terminal'

    gui.window._ensure_omni_search().activate_result(setting)
    gui.pump(600)
    prefs = gui.window._preferences_window
    assert prefs.get_visible()
    assert prefs._selected_page_name == 'terminal'
    focus = prefs.get_focus()
    target = next(row for _p, _g, row in prefs._iter_setting_rows()
                  if row.get_title() == 'Copy on selection')
    assert focus is not None and (focus is target or focus.is_ancestor(target))
