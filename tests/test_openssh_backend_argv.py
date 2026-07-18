"""Tests for OpenSSHSFTPManager._build_argv — native-path command assembly
(SFTP subsystem request + headless askpass wiring)."""

import types

from tests._fm_harness import _load_file_manager_module


def _stub_prepared(command, env=None, password=None):
    return types.SimpleNamespace(
        command=command,
        env=env or {},
        use_sshpass=False,
        password=password,
        use_askpass=bool((env or {}).get("SSH_ASKPASS")),
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
    argv, env = manager._build_argv()

    ctx = captured["ctx"]
    # The SFTP subsystem is requested via `-s` (option, before host) + the
    # subsystem name "sftp" (remote command, after host) → `ssh … -s host sftp`.
    # command_type is "sftp" so the builder applies NumberOfPasswordPrompts=1
    # (one-shot askpass) while still spawning `ssh -s … sftp`.
    assert ctx.command_type == "sftp"
    assert ctx.extra_args[0] == "-s"
    assert ctx.remote_command == "sftp"
    assert ctx.native_mode is True
    assert argv == ["ssh", "-F", "/cfg", "-s", "host", "sftp"]
    assert argv[0] != "sshpass"
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


def test_build_argv_uses_manager_password_for_askpass(monkeypatch):
    """Password from the file-manager dialog lives on manager._password; it must
    reach resolve_native_auth via connection.password (no TTY on the SFTP pipe)."""
    import sshpilot.ssh_connection_builder as scb

    conn = types.SimpleNamespace(
        nickname="host",
        hostname="host.example",
        username="user",
        auth_method=1,
    )
    _load_file_manager_module(monkeypatch)
    import sshpilot.file_manager.openssh_backend as ob

    captured = {}

    def fake_headless(prepared_env, connection, *, session_password=None, base_env=None):
        captured["session_password"] = session_password
        captured["connection"] = connection
        return {"SSH_ASKPASS": "/askpass", "SSH_ASKPASS_REQUIRE": "prefer"}

    monkeypatch.setattr(
        scb,
        "build_ssh_connection",
        lambda ctx: _stub_prepared(["ssh", "host", "sftp"], password=None),
    )
    monkeypatch.setattr(scb, "apply_headless_askpass_env", fake_headless)

    manager = ob.OpenSSHSFTPManager("host", "user", 22, connection=conn)
    manager._password = "dialog-pw"

    argv, env = manager._build_argv()

    assert conn.password == "dialog-pw"
    assert captured["session_password"] == "dialog-pw"
    assert argv == ["ssh", "host", "sftp"]
    assert env.get("SSH_ASKPASS")
    manager.close()


def test_build_argv_replaces_desktop_askpass_with_ours(monkeypatch):
    """A desktop askpass in the process env must not hijack the PTY-less worker;
    headless askpass installs sshPilot's helper instead."""
    import sshpilot.ssh_connection_builder as scb

    monkeypatch.setenv("SSH_ASKPASS", "/usr/bin/ksshaskpass")
    monkeypatch.setenv("SSH_ASKPASS_REQUIRE", "prefer")
    monkeypatch.setattr(
        scb, "build_ssh_connection",
        lambda ctx: _stub_prepared(["ssh", "host", "sftp"], env={"KEEP": "1"}),
    )
    monkeypatch.setattr(
        scb,
        "_askpass_env_for_connection",
        lambda *_a, **_k: {
            "SSH_ASKPASS": "/sshpilot/askpass",
            "SSH_ASKPASS_REQUIRE": "prefer",
        },
    )

    manager = _manager(monkeypatch)
    argv, env = manager._build_argv()

    assert env["SSH_ASKPASS"] == "/sshpilot/askpass"
    assert env["SSH_ASKPASS_REQUIRE"] == "prefer"
    assert env["KEEP"] == "1"
    assert argv[0] != "sshpass"
    manager.close()


def test_build_argv_never_wraps_sshpass(monkeypatch):
    import sshpilot.ssh_connection_builder as scb

    monkeypatch.setattr(
        scb,
        "build_ssh_connection",
        lambda ctx: _stub_prepared(
            ["ssh", "host", "sftp"],
            env={"SSH_ASKPASS": "/a", "SSH_ASKPASS_REQUIRE": "prefer"},
            password="pw",
        ),
    )

    manager = _manager(monkeypatch)
    argv, env = manager._build_argv()
    assert argv == ["ssh", "host", "sftp"]
    assert env.get("SSH_ASKPASS")
    manager.close()
