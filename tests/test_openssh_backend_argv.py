"""Tests for OpenSSHSFTPManager._build_argv — the native-path command assembly
(SFTP subsystem request + askpass/sshpass wiring), mirroring the paramiko auth
tests but for the OpenSSH backend."""

import types

from tests._fm_harness import _load_file_manager_module


def _stub_prepared(command, env=None, use_sshpass=False, password=None):
    return types.SimpleNamespace(
        command=command,
        env=env or {},
        use_sshpass=use_sshpass,
        password=password,
        use_askpass=False,
    )


def _manager(monkeypatch):
    _load_file_manager_module(monkeypatch)
    import sshpilot.file_manager.openssh_backend as ob

    return ob.OpenSSHSFTPManager(
        "host", "user", 22, connection=types.SimpleNamespace(nickname="host")
    )


def test_build_argv_requests_sftp_subsystem(monkeypatch):
    import sshpilot.ssh_connection_builder as scb

    captured = {}

    def fake_build(ctx):
        captured["ctx"] = ctx
        return _stub_prepared(["ssh", "-F", "/cfg", "-s", "host", "sftp"])

    monkeypatch.setattr(scb, "build_ssh_connection", fake_build)

    manager = _manager(monkeypatch)
    argv, env, cleanup = manager._build_argv()

    ctx = captured["ctx"]
    # The SFTP subsystem is requested via `-s` (option, before host) + the
    # subsystem name "sftp" (remote command, after host) → `ssh … -s host sftp`.
    assert ctx.command_type == "ssh"
    assert ctx.extra_args[0] == "-s"
    assert ctx.remote_command == "sftp"
    assert ctx.native_mode is True
    assert argv == ["ssh", "-F", "/cfg", "-s", "host", "sftp"]
    assert cleanup is None
    manager.close()


def test_build_argv_rides_control_master_socket(monkeypatch):
    """The worker appends ControlPath (without ControlMaster) so it rides a live
    master socket and silently direct-connects when the socket is absent."""
    import sshpilot.ssh_connection_builder as scb
    import sshpilot.ssh_multiplex as ssh_multiplex

    captured = {}

    def fake_build(ctx):
        captured["ctx"] = ctx
        return _stub_prepared(["ssh", "host", "sftp"])

    monkeypatch.setattr(scb, "build_ssh_connection", fake_build)

    manager = _manager(monkeypatch)
    manager._build_argv()
    extra = captured["ctx"].extra_args
    assert extra[:1] == ["-s"]
    assert "-o" in extra
    assert f"ControlPath={ssh_multiplex.control_path()}" in extra
    assert not any("ControlMaster" in str(a) for a in extra)

    # run_command's argv rides the socket too.
    manager._build_argv(remote_command="uname", extra_args=())
    extra = captured["ctx"].extra_args
    assert f"ControlPath={ssh_multiplex.control_path()}" in extra

    # use_mux=False (mux-refused retry) omits it entirely.
    manager._build_argv(use_mux=False)
    assert captured["ctx"].extra_args == ["-s"]
    manager.close()


def test_build_argv_wraps_sshpass_when_password_auth(monkeypatch):
    import sshpilot.ssh_connection_builder as scb
    import sshpilot.ssh_password_exec as spe

    monkeypatch.setattr(
        scb,
        "build_ssh_connection",
        lambda ctx: _stub_prepared(
            ["ssh", "host", "sftp"], env={"X": "1"}, use_sshpass=True, password="pw"
        ),
    )

    calls = {}

    def fake_wrap(argv, password, env=None):
        calls["argv"] = list(argv)
        calls["password"] = password
        return (["sshpass", "-f", "fifo"] + list(argv), lambda: calls.setdefault("cleaned", True))

    monkeypatch.setattr(spe, "wrap_argv_with_sshpass", fake_wrap)

    manager = _manager(monkeypatch)
    argv, env, cleanup = manager._build_argv()

    assert calls["password"] == "pw"
    assert argv[0] == "sshpass"
    assert argv[-3:] == ["ssh", "host", "sftp"]
    assert callable(cleanup)
    manager.close()


def test_build_argv_uses_manager_password_for_sshpass(monkeypatch):
    """Password from the file-manager dialog lives on manager._password; it must
    reach resolve_native_auth via connection.password (no TTY on the SFTP pipe)."""
    import sshpilot.ssh_password_exec as spe

    conn = types.SimpleNamespace(
        nickname="host",
        hostname="host.example",
        username="user",
        auth_method=1,
    )
    _load_file_manager_module(monkeypatch)
    import sshpilot.file_manager.openssh_backend as ob

    manager = ob.OpenSSHSFTPManager("host", "user", 22, connection=conn)
    manager._password = "dialog-pw"

    calls = {}

    def fake_wrap(argv, password, env=None):
        calls["password"] = password
        return (["sshpass", "-f", "fifo"] + list(argv), lambda: None)

    monkeypatch.setattr(spe, "wrap_argv_with_sshpass", fake_wrap)

    argv, env, cleanup = manager._build_argv()

    assert conn.password == "dialog-pw"
    assert calls["password"] == "dialog-pw"
    assert argv[0] == "sshpass"
    manager.close()


def test_build_argv_strips_cleared_askpass_from_env(monkeypatch):
    """The auth resolver clears SSH_ASKPASS by *omitting* it from its env; a
    plain merge with os.environ would resurrect a desktop askpass and let it
    hijack the PTY-less worker's prompts. The merge must honor the deletion."""
    import sshpilot.ssh_connection_builder as scb

    monkeypatch.setenv("SSH_ASKPASS", "/usr/bin/ksshaskpass")
    monkeypatch.setenv("SSH_ASKPASS_REQUIRE", "prefer")
    # Resolver returns an env WITHOUT askpass (the "nothing saved" branch).
    monkeypatch.setattr(
        scb, "build_ssh_connection",
        lambda ctx: _stub_prepared(["ssh", "host", "sftp"], env={"KEEP": "1"}),
    )

    manager = _manager(monkeypatch)
    argv, env, cleanup = manager._build_argv()

    assert "SSH_ASKPASS" not in env
    assert "SSH_ASKPASS_REQUIRE" not in env
    assert env["KEEP"] == "1"
    manager.close()


def test_build_argv_no_sshpass_when_key_auth(monkeypatch):
    import sshpilot.ssh_connection_builder as scb
    import sshpilot.ssh_password_exec as spe

    monkeypatch.setattr(
        scb,
        "build_ssh_connection",
        lambda ctx: _stub_prepared(["ssh", "host", "sftp"], use_sshpass=False),
    )

    def _boom(*a, **k):
        raise AssertionError("sshpass must not be used for key auth")

    monkeypatch.setattr(spe, "wrap_argv_with_sshpass", _boom)

    manager = _manager(monkeypatch)
    argv, env, cleanup = manager._build_argv()
    assert argv == ["ssh", "host", "sftp"]
    assert cleanup is None
    manager.close()
