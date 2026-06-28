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
    assert ctx.extra_args == ["-s"]
    assert ctx.remote_command == "sftp"
    assert ctx.native_mode is True
    assert argv == ["ssh", "-F", "/cfg", "-s", "host", "sftp"]
    assert cleanup is None
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
