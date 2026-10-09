import pytest
import shutil
import subprocess

from tests._gui_harness import requires_gui

requires_gui()

pytestmark = pytest.mark.gui


def _focus_is_within(window, widget):
    focused = window.get_focus()
    while focused is not None:
        if focused is widget:
            return True
        focused = focused.get_parent()
    return False


def test_omni_search_switches_between_welcome_anchor_and_center(gui):
    win = gui.window
    omni = win._omni_search

    assert win.is_start_tab_selected()
    assert omni.home.get_child() is omni.content

    omni.show()
    gui.pump(100)
    assert omni.popup.visible
    assert omni.popup.mode == "anchored"
    assert omni.content.get_parent() is omni.popup._panel
    assert _focus_is_within(win, omni.entry)

    omni.dismiss()
    gui.pump(100)
    assert not omni.popup.visible
    assert omni.home.get_child() is omni.content

    win.terminal_manager.show_local_terminal()
    gui.pump(200)
    assert not win.is_start_tab_selected()

    omni.show()
    gui.pump(100)
    assert omni.popup.visible
    assert omni.popup.mode == "omni"
    assert omni.content.get_parent() is omni.popup._panel
    assert _focus_is_within(win, omni.entry)

    omni.dismiss()


def test_omni_search_rebuilds_results_on_real_window(gui):
    omni = gui.window._omni_search
    omni.show()
    gui.pump(100)

    omni.entry.set_text("settings")
    gui.pump(300)

    row = omni.results.get_row_at_index(0)
    assert row is not None
    assert row.omni_result.kind == "command"
    assert row.omni_result.payload.action == "app.preferences"

    omni.dismiss()


def test_typing_in_docked_entry_opens_omni_and_keeps_keyboard_focus(gui):
    win = gui.window
    win.show_start_tab()
    gui.pump(100)
    omni = win._omni_search

    omni.entry.set_text("s")
    gui.pump(400)

    assert omni.popup.visible
    assert omni.popup.mode == "anchored"
    assert _focus_is_within(win, omni.entry)


def test_real_mouse_click_routes_typing_to_welcome_omni(gui):
    if shutil.which("xdotool") is None:
        pytest.skip("xdotool is required for pointer-event coverage")

    win = gui.window
    win.show_start_tab()
    gui.pump(100)
    omni = win._omni_search
    omni.dismiss(clear=True)
    win.search_entry.set_text("")
    gui.pump(100)

    try:
        import gi
        gi.require_version("GdkX11", "4.0")
        from gi.repository import GdkX11
        window_id = str(GdkX11.X11Surface.get_xid(win.get_surface()))
    except Exception as exc:
        pytest.skip(f"X11 surface unavailable: {exc}")
    geometry = subprocess.check_output(
        ["xdotool", "getwindowgeometry", "--shell", window_id],
        text=True,
    )
    values = dict(
        line.split("=", 1) for line in geometry.splitlines() if "=" in line
    )
    translated = omni.home.translate_coordinates(
        win, omni.home.get_width() // 2, omni.home.get_height() // 2,
    )
    if len(translated) == 2:
        x, y = translated
        ok = True
    else:
        ok, x, y = translated
    assert ok

    subprocess.run([
        "xdotool", "mousemove", "--sync",
        str(int(values["X"]) + int(x)),
        str(int(values["Y"]) + int(y)),
        "click", "1",
    ], check=True)
    gui.pump(150)
    subprocess.run(["xdotool", "type", "--delay", "20", "abc"], check=True)
    gui.pump(300)

    assert omni.popup.visible
    assert _focus_is_within(win, omni.entry)
    assert omni.entry.get_text() == "abc"
    assert win.search_entry.get_text() == ""


def test_start_activation_traces_omni_attention_once(gui):
    """Returning to the Start tab traces the docked omni-search border exactly
    once, without grabbing its keyboard focus, and the tracer retires by
    itself. Timing waits derive from the production duration, not arbitrary
    sleeps."""
    from sshpilot.omni_search import _ATTENTION_LAPS, _ATTENTION_MS

    win = gui.window
    omni = win._omni_search
    attention_total_ms = _ATTENTION_MS * _ATTENTION_LAPS
    omni.dismiss(clear=True)
    gui.pump(attention_total_ms + 300)  # let any startup tracer finish

    win.terminal_manager.show_local_terminal()
    gui.pump(200)
    assert not win.is_start_tab_selected()
    assert not omni.attention_active

    win.show_start_tab()
    gui.pump(50)
    assert win.is_start_tab_selected()
    assert omni.attention_active
    assert omni._attention_area.get_mapped()
    assert not _focus_is_within(win, omni.entry)

    gui.pump(attention_total_ms + 250)
    assert not omni.attention_active

    win.terminal_manager.show_local_terminal()
    gui.pump(200)
    assert not win.is_start_tab_selected()
    assert not omni.attention_active

    win.show_start_tab()
    gui.pump(50)
    assert omni.attention_active
    gui.pump(attention_total_ms + 250)
    assert not omni.attention_active


def test_attention_never_fires_when_start_already_selected(gui):
    """Re-selecting the current Start tab is not a transition: no tracer."""
    from sshpilot.omni_search import _ATTENTION_LAPS, _ATTENTION_MS

    win = gui.window
    omni = win._omni_search
    attention_total_ms = _ATTENTION_MS * _ATTENTION_LAPS
    omni.dismiss(clear=True)
    gui.pump(attention_total_ms + 300)
    assert win.is_start_tab_selected()
    assert not omni.attention_active

    win.show_start_tab()
    gui.pump(100)

    assert not omni.attention_active


def _top(win, query):
    from sshpilot.omni_search import search_omni

    return search_omni(win, query)[0]


@pytest.mark.parametrize("query, action", [
    ("identities", "win.manage-login-profiles"),
    ("identity", "win.manage-login-profiles"),
    ("prefs", "app.preferences"),
    ("settings", "app.preferences"),
    ("send to all", "app.broadcast-command"),
    ("split", "win.new-split-view"),
    ("snippets", "win.toggle-command-blocks"),
    ("hide sidebar", "win.toggle_sidebar"),
    ("dark mode", "win.set-app-theme"),
    ("keygen", "app.new-key"),
    ("ssh key", "app.new-key"),
    ("ssh config", "app.edit-ssh-config"),
    ("hotkeys", "app.shortcuts"),
    ("upgrade", "win.check-for-updates"),
    ("fullscreen", "win.toggle-fullscreen"),
    ("help", "app.help"),
    ("exit", "app.quit"),
])
def test_everyday_wording_reaches_the_feature(gui, query, action):
    result = _top(gui.window, query)
    assert result.kind == "command", result
    assert result.payload.action == action


def test_headerbar_button_switches_are_not_commands(gui):
    from sshpilot.omni_search import collect_commands

    actions = [c.action for c in collect_commands(gui.window)]
    assert not [a for a in actions if a.startswith("win.headerbar-")]
    assert "win.toggle_sidebar" in actions


def test_dark_mode_targets_the_dark_style(gui):
    assert _top(gui.window, "dark mode").payload.target.get_string() == "dark"


@pytest.mark.parametrize("query, page", [
    ("font", "terminal"),
    ("color scheme", "terminal"),
    ("keyring", "security-&-credentials"),
])
def test_settings_wording_opens_the_settings_page(gui, query, page):
    result = _top(gui.window, query)
    assert (result.kind, result.payload) == ("settings", page)

    gui.window._omni_search.activate_result(result)
    gui.pump(300)
    prefs = gui.window._preferences_window
    assert gui.window.nav_view.get_visible_page() is prefs
    assert prefs.content_stack.get_visible_child_name() == page


def test_dashboard_result_opens_the_host_dashboard(gui, monkeypatch):
    from sshpilot.omni_search import OmniResult

    opened = []
    host = object()
    monkeypatch.setattr(
        gui.window, "_open_dashboard_for_connection", opened.append,
    )
    gui.window._omni_search.activate_result(OmniResult(
        "dashboard", "web", "Dashboard", "info-outline-symbolic", 1400, host,
    ))
    assert opened == [host]
