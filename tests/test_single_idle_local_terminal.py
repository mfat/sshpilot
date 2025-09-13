import os
import signal
import subprocess
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
    repo.Gtk = Module(
        ApplicationWindow=type("ApplicationWindow", (), {}),
        Box=type("Box", (), {}),
        Orientation=types.SimpleNamespace(VERTICAL=0),
        ScrolledWindow=type("ScrolledWindow", (), {}),
        PolicyType=types.SimpleNamespace(AUTOMATIC=0),
    )
    repo.Adw = Module(ApplicationWindow=type("ApplicationWindow", (), {}))
    repo.Gio = Module()
    repo.GLib = Module(
        SpawnFlags=types.SimpleNamespace(DEFAULT=0),
        source_remove=lambda *a, **k: None,
    )
    repo.GObject = Module()
    repo.Gdk = Module()
    repo.Pango = Module()
    repo.PangoFT2 = Module()
    repo.Vte = Module(PtyFlags=types.SimpleNamespace(DEFAULT=0))

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

    import sshpilot.terminal as terminal

    class StubPty:
        def __init__(self, fd):
            self._fd = fd

        def get_slave_fd(self):
            return os.dup(self._fd)

    class StubVteTerminal:
        def __init__(self, widget):
            self.widget = widget
            self._pty = None
            self.child = None

        def get_pty(self):
            return self._pty

        def spawn_async(
            self,
            flags,
            working_dir,
            argv,
            envv,
            spawn_flags,
            child_setup,
            child_setup_data,
            timeout,
            cancellable,
            callback,
            user_data,
        ):
            master, slave = os.openpty()
            env = {k.split("=", 1)[0]: k.split("=", 1)[1] for k in envv}
            proc = subprocess.Popen(
                argv,
                stdin=slave,
                stdout=slave,
                stderr=slave,
                preexec_fn=os.setsid,
                env=env,
            )
            os.close(master)
            self.child = proc
            self._pty = StubPty(slave)
            callback(self.widget, proc.pid, None, user_data)

        def grab_focus(self):
            pass

    class DummyTerminal:
        has_active_foreground_job = terminal.TerminalWidget.has_active_foreground_job
        setup_local_shell = terminal.TerminalWidget.setup_local_shell
        _on_spawn_complete = terminal.TerminalWidget._on_spawn_complete

        def __init__(self):
            self.config = object()
            self.connection_manager = object()
            self.connection = types.SimpleNamespace(host="localhost")
            self.vte = StubVteTerminal(self)
            self.emit = lambda *a, **k: None
            self.setup_terminal = lambda: None
            self._set_connecting_overlay_visible = lambda *a, **k: None
            self.apply_theme = lambda: None
            self._set_disconnected_banner_visible = lambda *a, **k: None
            self.is_connected = False
            self._is_quitting = False
            self.session_id = "dummy"

    stub_modules = {
        'sshpilot.terminal_manager': types.SimpleNamespace(TerminalManager=lambda window: None),
        'sshpilot.connection_manager': types.SimpleNamespace(ConnectionManager=lambda: None, Connection=object),
        'sshpilot.config': types.SimpleNamespace(Config=lambda: types.SimpleNamespace(get_setting=lambda *a, **k: False)),
        'sshpilot.key_manager': types.SimpleNamespace(KeyManager=lambda: None, SSHKey=object),
        'sshpilot.connection_dialog': types.SimpleNamespace(ConnectionDialog=object),
        'sshpilot.preferences': types.SimpleNamespace(
            PreferencesWindow=object,
            is_running_in_flatpak=lambda: False,
            should_hide_external_terminal_options=lambda: False,
            should_hide_file_manager_options=lambda: False,
        ),
        'sshpilot.sshcopyid_window': types.SimpleNamespace(SshCopyIdWindow=object),
        'sshpilot.groups': types.SimpleNamespace(GroupManager=lambda config: None),
        'sshpilot.sidebar': types.SimpleNamespace(
            GroupRow=object,
            ConnectionRow=object,
            build_sidebar=lambda *a, **k: None,
        ),
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

    term = DummyTerminal()
    term.setup_local_shell()

    class DummyConn:
        nickname = "Local Terminal"

    class DummyWindow(Gtk.ApplicationWindow):
        on_close_request = window.MainWindow.on_close_request

    win = DummyWindow()
    win._is_quitting = False
    win.connection_to_terminals = {DummyConn(): [term]}

    called = {"dialog": False}

    def fake_show(self):
        called["dialog"] = True

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
        if term.vte.child:
            os.killpg(term.process_pgid, signal.SIGTERM)

    assert result is False
    assert called["dialog"] is False

