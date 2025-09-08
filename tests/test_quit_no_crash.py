import sys
import types
import importlib
import pytest

try:  # Skip test if Gtk/Adw bindings aren't available
    import gi
    gi.require_version('Gtk', '4.0')
    gi.require_version('Adw', '1')
    from gi.repository import Gtk, Adw, GLib
except Exception:  # pragma: no cover - environment without GI bindings
    pytest.skip("GTK or Adw not available", allow_module_level=True)


def test_application_quit_with_confirmation_dialog_does_not_crash():
    # Stub heavy application modules before importing window
    stub_modules = {
        'sshpilot.terminal': types.SimpleNamespace(TerminalWidget=object),
        'sshpilot.terminal_manager': types.SimpleNamespace(TerminalManager=lambda window: None),
        'sshpilot.connection_manager': types.SimpleNamespace(ConnectionManager=lambda: None, Connection=object),
        'sshpilot.config': types.SimpleNamespace(Config=lambda: types.SimpleNamespace(get_setting=lambda *a, **k: False)),
        'sshpilot.key_manager': types.SimpleNamespace(KeyManager=lambda: None, SSHKey=object),
        'sshpilot.connection_dialog': types.SimpleNamespace(ConnectionDialog=object),
        'sshpilot.askpass_utils': types.SimpleNamespace(ensure_askpass_script=lambda: None),
        'sshpilot.preferences': types.SimpleNamespace(PreferencesWindow=object, is_running_in_flatpak=lambda: False,
                                                      should_hide_external_terminal_options=lambda: False),
        'sshpilot.sshcopyid_window': types.SimpleNamespace(SshCopyIdWindow=object),
        'sshpilot.groups': types.SimpleNamespace(GroupManager=lambda config: None),
        'sshpilot.sidebar': types.SimpleNamespace(GroupRow=object, ConnectionRow=object, build_sidebar=lambda *a, **k: None),
        'sshpilot.sftp_utils': types.SimpleNamespace(open_remote_in_file_manager=lambda *a, **k: None),
        'sshpilot.welcome_page': types.SimpleNamespace(WelcomePage=object),
        'sshpilot.actions': types.SimpleNamespace(WindowActions=object, register_window_actions=lambda window: None),
        'sshpilot.shutdown': types.SimpleNamespace(cleanup_and_quit=lambda w: None),
        'sshpilot.search_utils': types.SimpleNamespace(connection_matches=lambda *a, **k: False),
    }
    old_modules = {}
    for name, mod in stub_modules.items():
        old_modules[name] = sys.modules.get(name)
        sys.modules[name] = mod

    import sshpilot.window as window

    class DummyWindow(Gtk.ApplicationWindow):
        show_quit_confirmation_dialog = window.MainWindow.show_quit_confirmation_dialog
        on_quit_confirmation_response = window.MainWindow.on_quit_confirmation_response

    class DummyApp(Gtk.Application):
        def quit(self):
            win = self.props.active_window
            if win and not getattr(win, '_is_quitting', False):
                if win.on_close_request(win):
                    return
            super().quit()

    app = DummyApp()
    holder = {'released': False, 'visible': False}
    original_alert = Adw.AlertDialog

    class CaptureDialog(original_alert):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            holder['dialog'] = self

    Adw.AlertDialog = CaptureDialog

    original_release = app.release

    def capture_release():
        holder['released'] = True
        return original_release()

    app.release = capture_release

    result = {'done': False}

    def on_activate(app):
        win = DummyWindow(application=app)
        class Conn: nickname = 'conn'
        class Term: is_connected = True
        win.connection_to_terminals = {Conn(): [Term()]}
        win.present()
        def respond():
            dialog = holder.get('dialog')
            if dialog is None:
                return True  # try again shortly
            dialog.emit('response', 'cancel')
            holder['visible'] = win.get_visible()
            result['done'] = True

            def exit_app():
                win._is_quitting = True
                app.quit()
                return False
            GLib.timeout_add(10, exit_app)
            return False

        GLib.timeout_add(50, respond)
        GLib.timeout_add(10, lambda: (app.quit(), False))

    app.connect('activate', on_activate)
    app.run(None)

    Adw.AlertDialog = original_alert
    for name, old in old_modules.items():
        if old is None:
            del sys.modules[name]
        else:
            sys.modules[name] = old

    assert result['done']
    assert holder['released']
    assert holder['visible']
