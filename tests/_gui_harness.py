"""Reusable harness for real-GTK GUI tests.

These tests boot the *real* ``SshPilotApplication`` on a display and drive it
through its real ``Gio`` actions / widgets, then assert on observable state
(open dialogs, tab counts, …). They are opt-in and never run in CI:

    SSHPILOT_GUI_TESTS=1 pytest -m gui          # on a display
    SSHPILOT_GUI_TESTS=1 xvfb-run -a pytest -m gui   # headless box

``requires_gui()`` makes them *skip* (never error) whenever real PyGObject, a
display, or the opt-in env var is missing — which is exactly the case in the
headless CI image (where ``tests/conftest.py`` stubs ``gi``), so the unit suite
stays green. See the plan in the repo for the full rationale.

Good fit: action/dialog/state/preference flows. Not a fit: pixel-gesture bugs,
drag-and-drop, VTE output scraping, or anything needing live SSH — use unit
tests for those.
"""

import gc
import os
import sys
import threading

import pytest


def requires_gui():
    """Skip the calling module unless real GTK + a display + the opt-in are present.

    Returns ``(Gtk, Adw, Gio, GLib)`` when the GUI environment is usable.
    Call it at module top level (it uses ``allow_module_level=True``).
    """
    if os.environ.get('SSHPILOT_GUI_TESTS') != '1':
        pytest.skip(
            'GUI tests are opt-in: set SSHPILOT_GUI_TESTS=1 (needs PyGObject + a display)',
            allow_module_level=True,
        )
    try:
        import gi

        gi.require_version('Gtk', '4.0')
        gi.require_version('Adw', '1')
        from gi.repository import Gtk, Adw, Gio, GLib
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f'PyGObject/GTK unavailable: {exc}', allow_module_level=True)

    # Reject the conftest stubs: real gi types live under the 'gi.repository'
    # module, the dynamic stub types do not. This is what keeps CI green even
    # though `import gi` there succeeds (against the stub).
    if not getattr(Gtk.ApplicationWindow, '__module__', '').startswith('gi.repository'):
        pytest.skip(
            'GTK is stubbed (headless/CI); real PyGObject not loaded',
            allow_module_level=True,
        )
    if not Gtk.init_check():
        pytest.skip('no display available for GTK', allow_module_level=True)

    return Gtk, Adw, Gio, GLib


class GuiApp:
    """Boots the real app once and exposes helpers to drive and observe it."""

    def __init__(self):
        from gi.repository import Gtk, Adw, Gio, GLib

        self.Gtk, self.Adw, self.Gio, self.GLib = Gtk, Adw, Gio, GLib
        self.app = None
        self.window = None
        self._saved_excepthook = None
        self._saved_threadhook = None

    # -- lifecycle ---------------------------------------------------------
    def boot(self, application_id: str | None = None):
        Gio = self.Gio
        # The app installs process-wide exception hooks; remember the originals
        # so teardown can restore them for the rest of the (non-GUI) suite.
        self._saved_excepthook = sys.excepthook
        self._saved_threadhook = getattr(threading, 'excepthook', None)

        from sshpilot.main import SshPilotApplication

        # Unique application_id must be set at construction — set_application_id
        # after Adw.Application.__init__ does not always rebind the D-Bus path.
        self.app = SshPilotApplication(
            isolated=True,
            application_id=application_id or 'io.github.mfat.sshpilot',
        )
        # NON_UNIQUE: don't hijack a running sshpilot over D-Bus and don't trip
        # single-instance behaviour inside the test process.
        self.app.set_flags(self.app.get_flags() | Gio.ApplicationFlags.NON_UNIQUE)
        self.app.register(None)
        self.app.activate()  # runs on_activate -> builds + presents MainWindow
        self.window = self.app.window
        assert self.window is not None, 'app.window not set after activate()'
        self.pump(300)
        return self

    def shutdown(self):
        try:
            self.reset()
        except Exception:
            pass
        # Remove the app's 5s GC timer and re-enable cyclic GC (the app disabled
        # it in __init__); otherwise later tests run with gc off.
        try:
            tid = getattr(self.app, '_gc_timer_id', None)
            if tid:
                self.GLib.source_remove(tid)
                self.app._gc_timer_id = None
        except Exception:
            pass
        gc.enable()
        if self._saved_excepthook is not None:
            sys.excepthook = self._saved_excepthook
        if self._saved_threadhook is not None:
            threading.excepthook = self._saved_threadhook
        try:
            if self.window is not None:
                self.window.close()
            self.pump(200)
        except Exception:
            pass
        # Fully quit so D-Bus exports are released before a second instance boots.
        try:
            if self.app is not None:
                self.app.quit()
                self.pump(300)
        except Exception:
            pass
        try:
            if self.app is not None:
                # Hold a strong ref until unref; clear window first.
                self.window = None
                app = self.app
                self.app = None
                # Drop last Python refs; Gio releases the exported object.
                del app
                gc.collect()
                self.pump(200)
        except Exception:
            self.app = None
            self.window = None

    # -- main loop ---------------------------------------------------------
    def pump(self, ms=300):
        """Run the default main context for ~``ms`` so queued idle/timeout work fires."""
        ctx = self.GLib.MainContext.default()
        done = {'v': False}
        self.GLib.timeout_add(ms, lambda: done.__setitem__('v', True) or False)
        while not done['v']:
            ctx.iteration(True)

    # -- observation -------------------------------------------------------
    def message_dialogs(self):
        """Currently-present ``Adw.MessageDialog`` toplevels."""
        Adw, Gtk = self.Adw, self.Gtk
        out = []
        tops = Gtk.Window.get_toplevels()
        for i in range(tops.get_n_items()):
            w = tops.get_item(i)
            if isinstance(w, Adw.MessageDialog):
                out.append(w)
        return out

    def all_pages(self):
        win = self.window
        return [win.tab_view.get_nth_page(i) for i in range(win.tab_view.get_n_pages())]

    def user_pages(self):
        """Pages excluding the pinned Start tab."""
        win = self.window
        return [p for p in self.all_pages() if not win._is_start_tab_page(p)]

    # -- driving -----------------------------------------------------------
    def open_local_tabs(self, n):
        for _ in range(n):
            self.window.terminal_manager.show_local_terminal()
        self.pump(400)

    def activate_action(self, name, target_page=None):
        """Activate a ``win.<name>`` action, optionally setting the tab-menu target first."""
        if target_page is not None:
            self.window._tab_menu_page = target_page
        self.window.lookup_action(name).activate(None)
        self.pump(300)

    def respond(self, response_id):
        """Send ``response_id`` to the (single) open message dialog."""
        dlgs = self.message_dialogs()
        assert dlgs, 'no message dialog present to respond to'
        dlgs[0].response(response_id)
        self.pump(300)

    def reset(self):
        """Close all user tabs so each test starts from a clean window."""
        win = self.window
        # Dismiss omni-search so content is re-docked into the welcome home
        # bin; otherwise a prior test can leave home empty and flake later.
        try:
            omni = getattr(win, '_omni_search', None)
            if omni is not None:
                omni.dismiss(clear=True)
        except Exception:
            pass
        # Leave Settings and close hosted Adw dialogs so a prior test cannot
        # leak NavigationPage state or an AlertDialog into the next one.
        try:
            if hasattr(win, 'is_preferences_visible') and win.is_preferences_visible():
                win.leave_preferences()
        except Exception:
            pass
        try:
            self._close_hosted_dialogs()
        except Exception:
            pass
        win._suppress_close_confirmation = True
        try:
            for p in list(win.tab_view.get_pages()):
                if not win._is_start_tab_page(p):
                    try:
                        win.tab_view.close_page(p)
                    except Exception:
                        pass
        finally:
            win._suppress_close_confirmation = False
        try:
            if hasattr(win, 'show_start_tab'):
                win.show_start_tab()
        except Exception:
            pass
        self.pump(300)

    def _close_hosted_dialogs(self):
        """Force-close any mapped ``Adw.Dialog`` still hosted on the main window."""
        Adw = self.Adw
        win = self.window
        if win is None:
            return

        def walk(widget):
            yield widget
            child = widget.get_first_child()
            while child is not None:
                yield from walk(child)
                child = child.get_next_sibling()

        for widget in list(walk(win)):
            if isinstance(widget, Adw.Dialog) and widget.get_mapped():
                try:
                    widget.force_close()
                except Exception:
                    try:
                        widget.close()
                    except Exception:
                        pass
        self.pump(100)


# The pytest fixtures (`gui`, `_gui_app_session`) live in tests/conftest.py, not
# here: a session-scoped fixture must be defined in one place (a conftest) so it
# is shared across every GUI test module. Importing it into each module would
# give each module its own FixtureDef and boot a second SshPilotApplication —
# which fails, since only one GApplication can register per process.
