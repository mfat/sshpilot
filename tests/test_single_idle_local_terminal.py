import sys
import types


def test_single_idle_local_terminal_allows_close():
    gi_module = types.ModuleType("gi")
    gi_module.require_version = lambda *a, **k: None

    class Module(types.SimpleNamespace):
        def __getattr__(self, name):
            return Module()

        def __call__(self, *a, **k):
            return Module()

    repo = Module()
    repo.Gtk = Module(ApplicationWindow=type("ApplicationWindow", (), {}))
    repo.Adw = Module()
    repo.Gio = Module()
    repo.GLib = Module()
    repo.GObject = Module()
    repo.Gdk = Module()
    repo.Pango = Module()
    repo.PangoFT2 = Module()
    repo.Vte = Module()

    gi_module.repository = repo
    original_gi = {
        name: sys.modules.get(name)
        for name in [
            "gi",
            "gi.repository",
            *[f"gi.repository.{n}" for n in [
                "Gtk",
                "Adw",
                "Gio",
                "GLib",
                "GObject",
                "Gdk",
                "Pango",
                "PangoFT2",
                "Vte",
            ]],
        ]
    }
    sys.modules["gi"] = gi_module
    sys.modules["gi.repository"] = repo
    for name in [
        "Gtk",
        "Adw",
        "Gio",
        "GLib",
        "GObject",
        "Gdk",
        "Pango",
        "PangoFT2",
        "Vte",
    ]:
        sys.modules[f"gi.repository.{name}"] = getattr(repo, name)

    Gtk = repo.Gtk


    stub_modules = {
        'sshpilot.terminal': types.SimpleNamespace(TerminalWidget=object),
        'sshpilot.terminal_manager': types.SimpleNamespace(TerminalManager=lambda window: None),
        'sshpilot.connection_manager': types.SimpleNamespace(ConnectionManager=lambda: None, Connection=object),
        'sshpilot.config': types.SimpleNamespace(Config=lambda: types.SimpleNamespace(get_setting=lambda *a, **k: False)),
        'sshpilot.key_manager': types.SimpleNamespace(KeyManager=lambda: None, SSHKey=object),
        'sshpilot.connection_dialog': types.SimpleNamespace(ConnectionDialog=object),
        'sshpilot.preferences': types.SimpleNamespace(PreferencesWindow=object, is_running_in_flatpak=lambda: False,
                                                      should_hide_external_terminal_options=lambda: False,
                                                      should_hide_file_manager_options=lambda: False),
        'sshpilot.sshcopyid_window': types.SimpleNamespace(SshCopyIdWindow=object),
        'sshpilot.groups': types.SimpleNamespace(GroupManager=lambda config: None),
        'sshpilot.sidebar': types.SimpleNamespace(GroupRow=object, ConnectionRow=object, build_sidebar=lambda *a, **k: None),
        'sshpilot.sftp_utils': types.SimpleNamespace(open_remote_in_file_manager=lambda *a, **k: None),
        'sshpilot.welcome_page': types.SimpleNamespace(WelcomePage=object),
        'sshpilot.actions': types.SimpleNamespace(WindowActions=object, register_window_actions=lambda window: None),
        'sshpilot.shutdown': types.SimpleNamespace(cleanup_and_quit=lambda w: None),
        'sshpilot.search_utils': types.SimpleNamespace(connection_matches=lambda *a, **k: False),
        'sshpilot.shortcut_utils': types.SimpleNamespace(get_primary_modifier_label=lambda: "Ctrl"),
        'sshpilot.platform_utils': types.SimpleNamespace(is_macos=lambda: False),
    }

    old_modules = {}
    for name, mod in stub_modules.items():
        old_modules[name] = sys.modules.get(name)
        sys.modules[name] = mod

    import sshpilot.window as window

    class DummyTerm:
        is_connected = False

        def has_active_foreground_job(self):
            return False

    class DummyConn:
        nickname = 'Local Terminal'

    class DummyWindow(Gtk.ApplicationWindow):
        on_close_request = window.MainWindow.on_close_request

    win = DummyWindow()
    win._is_quitting = False
    win.connection_to_terminals = {DummyConn(): [DummyTerm()]}

    called = {'dialog': False}

    def fake_show(self):
        called['dialog'] = True

    original_show = window.MainWindow.show_quit_confirmation_dialog
    window.MainWindow.show_quit_confirmation_dialog = fake_show

    try:
        result = win.on_close_request(win)
    finally:
        window.MainWindow.show_quit_confirmation_dialog = original_show
        for name, old in old_modules.items():
            if old is None:
                del sys.modules[name]
            else:
                sys.modules[name] = old
        for name, old in original_gi.items():
            if old is None:
                del sys.modules[name]
            else:
                sys.modules[name] = old


    assert result is False
    assert called['dialog'] is False
