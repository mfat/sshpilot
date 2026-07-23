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
