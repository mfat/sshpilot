"""GUI regression tests for the Settings window.

Settings is a separate, non-modal ``Adw.Window`` owned by a main window. Easy
silent regressions: dialogs parented to the wrong window (and opening behind
Settings, or on it after it was closed), app actions resolving the Settings
window instead of the main window, or the window not being reused.

These boot the real app and open the real Settings page. Opt-in only:
SSHPILOT_GUI_TESTS=1 pytest -m gui
"""

import pytest

from tests._gui_harness import requires_gui  # the `gui` fixture comes from conftest

requires_gui()

pytestmark = pytest.mark.gui


def _open_preferences(gui, page_id=None):
    gui.window.show_preferences(page_id)
    gui.pump(500)
    return getattr(gui.window, '_preferences_window', None)


def test_preferences_is_a_non_modal_window_above_the_main_window(gui):
    from gi.repository import Adw

    prefs = _open_preferences(gui)
    assert prefs is not None, 'show_preferences() did not store the window'
    assert isinstance(prefs, Adw.Window), f'expected Adw.Window, got {type(prefs)}'
    assert prefs is not gui.window
    assert prefs.get_visible()
    assert not prefs.get_modal()
    # Transient so it cannot fall behind the main window.
    assert prefs.get_transient_for() is gui.window
    assert prefs.parent_window is gui.window
    # Registered, so routed prompts can find it as the active window.
    assert prefs in gui.app.get_windows()
    assert gui.window.is_preferences_visible()


def test_preferences_close_hides_and_reuses_the_window(gui):
    first = _open_preferences(gui)
    assert first is not None

    again = _open_preferences(gui)
    assert again is first

    first.close()
    gui.pump(300)
    assert not first.get_visible()
    assert not gui.window.is_preferences_visible()
    # Close only hides: the instance (and its built pages) is kept for reuse.
    assert getattr(gui.window, '_preferences_window', None) is first

    assert _open_preferences(gui) is first
    assert gui.window.leave_preferences()
    gui.pump(300)
    assert not first.get_visible()


def test_preferences_select_page_deep_link(gui):
    prefs = _open_preferences(gui, page_id='plugins')
    assert prefs is not None
    assert prefs._selected_page_name == 'plugins'
    assert prefs.content_stack.get_visible_child_name() == 'plugins'


def test_appearance_and_sidebar_have_their_own_pages(gui):
    """Theme, accent and group styling live on Appearance; sidebar options on Sidebar."""
    from gi.repository import Adw

    prefs = _open_preferences(gui, page_id='appearance')
    assert prefs is not None
    _open_preferences(gui, page_id='sidebar')

    ids = []
    row = prefs.sidebar.get_first_child()
    while row is not None:
        ids.append(row.get_name())
        row = row.get_next_sibling()
    assert ids[:4] == ['interface', 'appearance', 'sidebar', 'terminal']

    appearance = prefs.pages['appearance']
    sidebar = prefs.pages['sidebar']
    assert prefs.theme_row.get_ancestor(Adw.PreferencesPage) is appearance
    assert prefs.accent_color_row.get_ancestor(Adw.PreferencesPage) is appearance
    assert prefs.group_color_display_row.get_ancestor(Adw.PreferencesPage) is appearance
    assert prefs.group_preview_list.get_ancestor(Adw.PreferencesPage) is appearance
    assert 'groups' not in ids
    assert prefs.sidebar_mode_row.get_ancestor(Adw.PreferencesPage) is sidebar

    group_titles = [
        r.get_ancestor(Adw.PreferencesGroup).get_title()
        for r in prefs._sidebar_mode_dependent_rows
    ]
    assert set(group_titles) == {'Layout', 'Row Details', 'Row Buttons'}

    config = gui.window.config
    old_flat = config.get_setting('ui.sidebar_flat_rows', False)
    try:
        prefs.sidebar_row_style_row.set_selected(1)
        assert config.get_setting('ui.sidebar_flat_rows') is True
        prefs.sidebar_row_style_row.set_selected(0)
        assert config.get_setting('ui.sidebar_flat_rows') is False
    finally:
        config.set_setting('ui.sidebar_flat_rows', old_flat)


def test_group_preview_follows_group_settings_live(gui):
    """The preview is built from real sidebar rows and rebuilds on a settings change."""
    from sshpilot.sidebar import ConnectionRow, GroupRow

    prefs = _open_preferences(gui, page_id='appearance')
    config = gui.window.config

    def rows():
        found, row = [], prefs.group_preview_list.get_first_child()
        while row is not None:
            found.append(row)
            row = row.get_next_sibling()
        return found

    old_mode = config.get_setting('ui.group_color_display', 'dot')
    try:
        config.set_setting('ui.group_color_display', 'fill')
        gui.pump(200)
        built = rows()
        assert [type(r) for r in built if isinstance(r, (GroupRow, ConnectionRow))] == [
            GroupRow, ConnectionRow, ConnectionRow, GroupRow, ConnectionRow,
        ]
        assert built[0].has_css_class('tinted')

        config.set_setting('ui.group_color_display', 'bar')
        gui.pump(200)
        rebuilt = rows()
        assert rebuilt[0] is not built[0], 'preview did not rebuild'
        assert not rebuilt[0].has_css_class('tinted')
        assert rebuilt[0].has_css_class('color-bar')
    finally:
        config.set_setting('ui.group_color_display', old_mode)


def test_layout_cards_and_startup_toggles_stay_in_sync(gui):
    """A layout card writes the startup settings the other pages show, and back."""
    prefs = _open_preferences(gui, page_id='interface')
    for page_id in ('sidebar',):
        prefs._ensure_page_built(page_id)
    config = gui.window.config
    chooser = prefs.layout_preset_chooser

    def selected():
        return [k for k, card in chooser._cards.items() if card.get_active()]

    old = (config.get_setting('app-startup-behavior', 'welcome'),
           config.get_setting('ui.sidebar_hide_on_startup', False))
    try:
        chooser._cards['terminal'].set_active(True)
        gui.pump(100)
        assert config.get_setting('app-startup-behavior') == 'terminal'
        assert config.get_setting('ui.sidebar_hide_on_startup') is True
        assert prefs.terminal_startup_radio.get_active()
        assert prefs.hide_on_startup_switch.get_active()
        assert selected() == ['terminal']

        prefs.hide_on_startup_switch.set_active(False)
        gui.pump(100)
        assert selected() == [], 'terminal with a visible sidebar is no preset'

        prefs.welcome_startup_radio.set_active(True)
        gui.pump(100)
        assert selected() == ['sidebar']
        assert not prefs.terminal_startup_radio.get_active()

        prefs.previous_session_startup_radio.set_active(True)
        gui.pump(100)
        assert selected() == []
    finally:
        config.set_setting('app-startup-behavior', old[0])
        config.set_setting('ui.sidebar_hide_on_startup', old[1])


def test_dialogs_from_settings_are_parented_to_settings(gui):
    """Page widgets resolve to the Settings window, so their dialogs stack on it."""
    from gi.repository import Gtk
    from sshpilot.window_dialogs import parent_window

    prefs = _open_preferences(gui)
    assert parent_window(prefs) is prefs
    assert prefs._dialog_parent() is prefs

    def walk(widget):
        yield widget
        child = widget.get_first_child()
        while child is not None:
            yield from walk(child)
            child = child.get_next_sibling()

    widgets = list(walk(prefs))
    assert len(widgets) > 50, f'only {len(widgets)} widgets; page tree did not build'
    for w in widgets:
        assert w.get_root() is prefs, f'{type(w).__name__} resolved {type(w.get_root())}'

    page = getattr(prefs, 'shortcuts_editor_page', None)
    if page is not None:
        assert page._transient_parent is prefs


def test_dialog_finishing_after_settings_closed_goes_to_main_window(gui, monkeypatch):
    """A plugin download that fails after Settings closed must not alert on a hidden window."""
    import time
    from gi.repository import Adw
    import sshpilot.plugins.registry_client as registry_client

    def slow_fail(*_args, **_kwargs):
        time.sleep(0.3)
        raise RuntimeError('network down')

    monkeypatch.setattr(registry_client, 'download_package', slow_fail)
    prefs = _open_preferences(gui, page_id='plugins')
    prefs._start_registry_install({'download_url': 'x', 'checksum_url': 'y'}, lambda: None)
    prefs.close()

    def hosted_alerts(root):
        found, stack = [], [root]
        while stack:
            widget = stack.pop()
            if isinstance(widget, Adw.AlertDialog) and widget.get_mapped():
                found.append(widget)
            child = widget.get_first_child()
            while child is not None:
                stack.append(child)
                child = child.get_next_sibling()
        return found

    alert = None
    for _ in range(20):
        gui.pump(100)
        alert = next(iter(hosted_alerts(gui.window)), None)
        if alert is not None:
            break
    assert not hosted_alerts(prefs), 'the alert went to the hidden Settings window'
    assert alert is not None, 'the failure alert was not shown on the main window'
    assert alert.get_root() is gui.window
    alert.force_close()
    gui.pump(200)


def test_routed_prompts_stack_on_active_settings(gui):
    """With Settings active, a daemon prompt (vault unlock) is presented on it."""
    from sshpilot.window_dialogs import resolve_topmost_prompt_parent

    prefs = _open_preferences(gui)
    target = resolve_topmost_prompt_parent(gui.app.get_windows(), prefs, gui.window)
    assert target is prefs


def test_app_actions_from_settings_act_on_the_main_window(gui):
    """App accelerators pressed in Settings must reach MainWindow, not Settings."""
    import types

    prefs = _open_preferences(gui)
    fake_app = types.SimpleNamespace(
        props=types.SimpleNamespace(active_window=prefs), window=gui.window)
    # The Settings action itself (Ctrl+, pressed in Settings) keeps it shown.
    assert type(gui.app)._action_window(fake_app, keep_settings=True) is gui.window
    assert prefs.get_visible()
    # Any other action (Ctrl+Q's quit confirmation, a new tab) shows its
    # result on the main window, which the transient Settings would cover.
    assert type(gui.app)._action_window(fake_app) is gui.window
    gui.pump(200)
    assert not prefs.get_visible(), 'Settings still covers the main window'


def test_plugin_page_leaves_settings(gui, monkeypatch):
    """A plugin's settings tab opens in the main window, so Settings must step aside."""
    import types

    prefs = _open_preferences(gui, page_id='plugins')
    opened = []
    monkeypatch.setattr(gui.window, 'plugin_host', types.SimpleNamespace(
        ui=types.SimpleNamespace(open_page=opened.append)), raising=False)
    prefs._open_plugin_page('example.page')
    gui.pump(200)
    assert opened == ['example.page']
    assert not prefs.get_visible(), 'the plugin tab opened behind Settings'


def test_toasts_show_on_settings_while_it_is_shown(gui):
    """Settings' own toasts, and routed pre-connection alerts, land on Settings."""
    prefs = _open_preferences(gui)
    assert prefs.toast_overlay is not None
    assert gui.app._pre_command_toast_overlay() is prefs.toast_overlay

    shown = []
    prefs.toast_overlay.add_toast = shown.append
    gui.window.toast_overlay.add_toast = lambda toast: shown.append(('main', toast))
    prefs._show_toast('on settings')
    assert len(shown) == 1 and shown[0].get_title() == 'on settings'

    # Hidden: its toasts go to the main window rather than nowhere.
    prefs.close()
    gui.pump(200)
    prefs._show_toast('on main')
    assert shown[-1][0] == 'main' and shown[-1][1].get_title() == 'on main'


def test_show_content_false_when_split_not_collapsed(gui):
    """Wide layout: Back must pop Settings on the first press (issue #1091)."""
    prefs = _open_preferences(gui)
    split = prefs.split_view

    split.set_collapsed(False)
    gui.pump(200)
    assert not split.get_show_content(), (
        'show_content must stay false side-by-side so Back reaches the outer NavigationView'
    )


def test_show_content_true_when_split_collapsed(gui):
    prefs = _open_preferences(gui)
    split = prefs.split_view

    split.set_collapsed(True)
    gui.pump(200)
    assert split.get_show_content(), 'collapsed Settings should show the selected page'


def test_collapsed_split_shows_content_on_page_change(gui):
    """Narrow layout: picking a category must show the detail page."""
    prefs = _open_preferences(gui)
    split = prefs.split_view

    split.set_collapsed(True)
    split.set_show_content(False)
    gui.pump(200)
    assert not split.get_show_content()

    current = prefs.sidebar.get_selected_row()
    other = next(
        r for i in range(10)
        if (r := prefs.sidebar.get_row_at_index(i)) is not None and r is not current
    )
    prefs.sidebar.select_row(other)
    gui.pump(200)
    assert split.get_show_content(), 'choosing a page did not show content'

    # Re-selecting the current page must not bounce show_content when already on list.
    split.set_show_content(False)
    gui.pump(100)
    prefs.sidebar.select_row(other)
    gui.pump(200)
    assert not split.get_show_content(), 'reselecting the same row forced content'


def _assert_alert_hosted_on_settings(gui, prefs, alert):
    """Shared checks: presented into the Settings window, topmost, interactive."""
    from gi.repository import Adw

    assert alert.get_visible()
    assert alert.get_mapped()
    assert prefs.get_visible(), 'alert should not close Settings'
    assert alert.get_root() is prefs

    host = alert.get_parent()
    assert host is not None, 'alert was not hosted in the window'
    assert type(host).__name__ == 'AdwDialogHost', (
        f'expected AdwDialogHost parent, got {type(host).__name__}'
    )
    last_dialog = None
    child = host.get_first_child()
    while child is not None:
        if isinstance(child, Adw.Dialog):
            last_dialog = child
        child = child.get_next_sibling()
    assert last_dialog is alert, 'alert is not the topmost hosted dialog'
    alert.grab_focus()
    gui.pump(100)
    assert alert.get_focus() is not None, 'hosted alert cannot accept focus'


def test_dialog_presented_from_settings_is_hosted_on_it(gui):
    from gi.repository import Adw

    prefs = _open_preferences(gui)
    alert = Adw.AlertDialog(heading='Unlock', body='presented from Settings')
    alert.add_response('ok', 'OK')
    alert.present(prefs._dialog_parent())
    gui.pump(300)

    _assert_alert_hosted_on_settings(gui, prefs, alert)

    alert.close()
    gui.pump(200)


def test_dialog_stacks_after_settings_split_churn(gui):
    """Collapsing and re-selecting Settings rows must not bury its dialogs."""
    from gi.repository import Adw

    prefs = _open_preferences(gui)
    split = prefs.split_view
    split.set_collapsed(True)
    gui.pump(200)
    current = prefs.sidebar.get_selected_row()
    other = next(
        r for i in range(10)
        if (r := prefs.sidebar.get_row_at_index(i)) is not None and r is not current
    )
    prefs.sidebar.select_row(other)
    gui.pump(200)
    split.set_show_content(False)
    gui.pump(100)
    prefs.sidebar.select_row(other)
    gui.pump(200)

    alert = Adw.AlertDialog(heading='Unlock', body='after split churn')
    alert.add_response('ok', 'OK')
    alert.present(prefs._dialog_parent())
    gui.pump(300)

    _assert_alert_hosted_on_settings(gui, prefs, alert)

    alert.close()
    split.set_collapsed(False)
    gui.pump(200)
