import importlib
import sys
import types


sys.modules.setdefault("cairo", types.ModuleType("cairo"))


def test_ssh_copy_id_saved_passphrase_uses_askpass(monkeypatch, tmp_path):
    # ssh-copy-id now resolves auth via the shared resolve_native_auth: a saved
    # key passphrase -> askpass (REQUIRE=prefer), same as the terminal and SCP.
    window_mod = importlib.import_module("sshpilot.window")
    askpass_mod = importlib.import_module("sshpilot.askpass_utils")
    scb = importlib.import_module("sshpilot.ssh_connection_builder")
    # The ssh-copy-id runner now lives in sshcopyid_window; patch its symbols.
    runner_mod = importlib.import_module("sshpilot.sshcopyid_window")

    class DummyWidget:
        def __init__(self, *args, **kwargs):
            self.children = []

        def append(self, child):
            self.children.append(child)
            return None

        def connect(self, *args, **kwargs):
            return None

        def __getattr__(self, name):
            def _method(*args, **kwargs):
                return None

            return _method

    class DummyVte:
        def __init__(self):
            self.spawn_env = None
            self.spawn_args = None

        def feed(self, *args, **kwargs):
            return None

        def spawn_async(self, *args):
            self.spawn_args = args
            if len(args) > 3:
                self.spawn_env = args[3]
            return None

        def connect(self, *args, **kwargs):
            return None

        def get_text_range(self, *args, **kwargs):
            return ("",)

    class DummyTerminalWidget:
        last_instance = None

        def __init__(self, connection, config, manager):
            self.connection = connection
            self.config = config
            self.connection_manager = manager
            self.vte = DummyVte()
            DummyTerminalWidget.last_instance = self

        def _set_connecting_overlay_visible(self, *args, **kwargs):
            return None

        def _set_disconnected_banner_visible(self, *args, **kwargs):
            return None

        def set_hexpand(self, *args, **kwargs):
            return None

        def set_vexpand(self, *args, **kwargs):
            return None

        def disconnect(self, *args, **kwargs):
            return None

    class DummyConfig:
        def get_ssh_config(self):
            return {}

    class DummyManager:
        known_hosts_path = None
        identity_agent_disabled = False

        def get_password(self, *args, **kwargs):
            return None

        def get_key_passphrase(self, *args, **kwargs):
            return "cached-secret"

        def store_key_passphrase(self, *args, **kwargs):
            return None

    forced_env = {
        "SSH_ASKPASS": "/tmp/helper",
        "SSH_ASKPASS_REQUIRE": "force",
        "DISPLAY": ":1",
    }
    prefer_env = {
        "SSH_ASKPASS": "/tmp/helper",
        "SSH_ASKPASS_REQUIRE": "prefer",
        "DISPLAY": ":1",
    }

    monkeypatch.setattr(runner_mod, "TerminalWidget", DummyTerminalWidget)
    monkeypatch.setattr(
        runner_mod,
        "_build_copy_progress_row",
        lambda *_args, **_kwargs: (
            DummyWidget(),
            lambda: False,
            lambda: None,
            lambda: None,
            lambda: None,
        ),
    )
    monkeypatch.setattr(
        runner_mod,
        "_build_terminal_disclosure",
        lambda *_args, **_kwargs: (
            DummyWidget(),
            lambda _expanded: None,
            lambda: False,
        ),
    )
    monkeypatch.setattr(
        runner_mod,
        "Adw",
        types.SimpleNamespace(
            ActionRow=DummyWidget,
            AlertDialog=DummyWidget,
            Dialog=types.SimpleNamespace(new=lambda: DummyWidget()),
            PreferencesGroup=DummyWidget,
            ToolbarView=DummyWidget,
            HeaderBar=DummyWidget,
            MessageDialog=DummyWidget,
            WindowTitle=types.SimpleNamespace(new=lambda *args, **kwargs: DummyWidget()),
        ),
        raising=False,
    )
    monkeypatch.setattr(
        runner_mod,
        "Gtk",
        types.SimpleNamespace(
            Box=DummyWidget,
            Label=DummyWidget,
            Button=DummyWidget,
            Align=types.SimpleNamespace(START=0, END=1, CENTER=3),
            Fixed=DummyWidget,
            Orientation=types.SimpleNamespace(VERTICAL=0, HORIZONTAL=1),
        ),
        raising=False,
    )
    monkeypatch.setattr(
        runner_mod,
        "GLib",
        types.SimpleNamespace(
            shell_quote=lambda value: value,
            SpawnFlags=types.SimpleNamespace(DEFAULT=0),
            idle_add=lambda func, *args, **kwargs: func(*args, **kwargs) or 1,
            timeout_add=lambda *args, **kwargs: 1,
            source_remove=lambda *_args: None,
            SOURCE_REMOVE=False,
            SOURCE_CONTINUE=True,
        ),
        raising=False,
    )
    monkeypatch.setattr(
        runner_mod,
        "Vte",
        types.SimpleNamespace(PtyFlags=types.SimpleNamespace(DEFAULT=0)),
        raising=False,
    )
    monkeypatch.setattr(runner_mod, "Config", DummyConfig, raising=False)
    monkeypatch.setattr(runner_mod, "ensure_writable_ssh_home", lambda env: None, raising=False)
    monkeypatch.setattr(
        runner_mod.shutil,
        "which",
        lambda name: "/usr/bin/ssh-copy-id" if name == "ssh-copy-id" else None,
    )

    monkeypatch.setattr(
        askpass_mod,
        "get_ssh_env_with_forced_askpass",
        lambda: forced_env.copy(),
        raising=False,
    )
    monkeypatch.setattr(
        askpass_mod,
        "get_ssh_env_with_askpass",
        lambda: prefer_env.copy(),
        raising=False,
    )
    monkeypatch.setattr(askpass_mod, "lookup_passphrase", lambda *_: "", raising=False)

    # resolve_native_auth (in ssh_connection_builder) decides the auth: make the
    # key look like it has a saved passphrase -> askpass(prefer).
    monkeypatch.setattr(scb, "lookup_passphrase", lambda *_: "pp", raising=False)
    monkeypatch.setattr(
        scb, "get_ssh_env_with_askpass", lambda require="prefer": prefer_env.copy(), raising=False
    )

    private_path = tmp_path / "id_test"
    private_path.write_text("private")
    public_path = private_path.with_suffix(private_path.suffix + ".pub")
    public_path.write_text("public")

    ssh_key = types.SimpleNamespace(
        private_path=str(private_path),
        public_path=str(public_path),
    )

    connection = types.SimpleNamespace(
        username="demo",
        hostname="example.com",
        host="",
        port=22,
        auth_method=0,
        key_passphrase="",
        keyfile=str(private_path),
        key_select_mode=0,
        identity_agent_disabled=True,
        pubkey_auth_no=False,
        resolved_identity_files=[str(private_path)],
    )

    manager = DummyManager()

    window_instance = window_mod.MainWindow.__new__(window_mod.MainWindow)
    window_instance.connection_manager = manager
    window_instance.config = DummyConfig()
    window_instance.sshcopyid_runner = runner_mod.SshCopyIdRunner(window_instance)

    window_instance._show_ssh_copy_id_terminal_using_main_widget(connection, ssh_key)

    spawned_env = DummyTerminalWidget.last_instance.vte.spawn_env
    assert spawned_env is not None
    assert "SSH_ASKPASS_REQUIRE=prefer" in spawned_env


def test_ssh_copy_id_preflight_blocks_missing_binary(monkeypatch, tmp_path):
    window_mod = importlib.import_module("sshpilot.window")
    runner_mod = importlib.import_module("sshpilot.sshcopyid_window")

    monkeypatch.setattr(window_mod.shutil, "which", lambda name: None)
    monkeypatch.setattr(window_mod.os.path, "exists", lambda path: False)

    private_path = tmp_path / "id_test"
    private_path.write_text("private")
    public_path = private_path.with_suffix(private_path.suffix + ".pub")
    public_path.write_text("public")

    ssh_key = types.SimpleNamespace(
        private_path=str(private_path),
        public_path=str(public_path),
    )
    connection = types.SimpleNamespace(
        username="demo",
        hostname="example.com",
        host="",
        port=22,
        auth_method=0,
    )

    errors = []
    window_instance = window_mod.MainWindow.__new__(window_mod.MainWindow)
    window_instance._error_dialog = lambda *args: errors.append(args)
    window_instance.sshcopyid_runner = runner_mod.SshCopyIdRunner(window_instance)

    window_instance._show_ssh_copy_id_terminal_using_main_widget(connection, ssh_key)

    assert errors
    assert errors[0][0] == "SSH Key Copy Error"
    assert "ssh-copy-id" in errors[0][1]


def test_ssh_copy_id_preflight_rejects_unreadable_public_key(monkeypatch, tmp_path):
    window_mod = importlib.import_module("sshpilot.window")
    runner_mod = importlib.import_module("sshpilot.sshcopyid_window")

    monkeypatch.setattr(
        window_mod.shutil,
        "which",
        lambda name: "/usr/bin/ssh-copy-id" if name == "ssh-copy-id" else None,
    )
    monkeypatch.setattr(runner_mod, "ensure_writable_ssh_home", lambda env: None, raising=False)

    public_path = tmp_path / "id_test.pub"
    public_path.write_text("public")
    monkeypatch.setattr(window_mod.os, "access", lambda path, mode: False)

    ssh_key = types.SimpleNamespace(public_path=str(public_path))
    connection = types.SimpleNamespace(
        username="demo",
        hostname="example.com",
        host="",
        port=22,
        auth_method=0,
    )

    window_instance = window_mod.MainWindow.__new__(window_mod.MainWindow)
    runner = runner_mod.SshCopyIdRunner(window_instance)
    result = runner._preflight(connection, ssh_key)

    assert result is not None
    assert result[0] == "Public key file is not readable"


def test_copyid_verdict_password_retry_is_success():
    # A mistyped-then-corrected password leaves "Permission denied" on screen,
    # but ssh-copy-id's own success message outranks the failure markers.
    runner_mod = importlib.import_module("sshpilot.sshcopyid_window")

    content = (
        "demo@example.com's password: \n"
        "Permission denied, please try again.\n"
        "demo@example.com's password: \n"
        "Number of key(s) added: 1\n"
    )
    assert runner_mod._copyid_run_succeeded(0, content)


def test_copyid_verdict_already_installed_is_success():
    runner_mod = importlib.import_module("sshpilot.sshcopyid_window")

    content = (
        "WARNING: All keys were skipped because they already exist "
        "on the remote system.\n"
    )
    assert runner_mod._copyid_run_succeeded(0, content)


def test_copyid_verdict_failure_markers_veto_zero_exit():
    runner_mod = importlib.import_module("sshpilot.sshcopyid_window")

    content = "demo@example.com: Permission denied (publickey,password).\n"
    assert not runner_mod._copyid_run_succeeded(0, content)


def test_copyid_verdict_nonzero_exit_is_failure():
    runner_mod = importlib.import_module("sshpilot.sshcopyid_window")

    assert not runner_mod._copyid_run_succeeded(1, "Number of key(s) added: 1\n")
    assert not runner_mod._copyid_run_succeeded(255, "")


def test_copyid_verdict_clean_zero_exit_is_success():
    runner_mod = importlib.import_module("sshpilot.sshcopyid_window")

    assert runner_mod._copyid_run_succeeded(0, "")


def test_terminal_awaiting_input_detects_prompts():
    runner_mod = importlib.import_module("sshpilot.sshcopyid_window")

    positives = [
        "demo@example.com's password: ",
        "Password:",
        "Enter passphrase for key '/home/u/.ssh/id_ed25519':",
        "Are you sure you want to continue connecting (yes/no/[fingerprint])? ",
        "Verification code:",
        "Enter PIN for authenticator:",
        # Prompt as the last line after earlier output.
        "Running ssh-copy-id\nINFO: attempting to log in\ndemo@host's password: ",
    ]
    for text in positives:
        assert runner_mod._terminal_awaiting_input(text), text


def test_terminal_awaiting_input_ignores_non_prompts():
    runner_mod = importlib.import_module("sshpilot.sshcopyid_window")

    negatives = [
        "",
        "Running ssh-copy-id…",
        "Number of key(s) added: 1",
        "demo@example.com: Permission denied (publickey,password).",
        "INFO: attempting to log in with the new key(s)",
        # Prompt no longer the last line once later output arrives.
        "demo@host's password: \nNumber of key(s) added: 1",
    ]
    for text in negatives:
        assert not runner_mod._terminal_awaiting_input(text), text
