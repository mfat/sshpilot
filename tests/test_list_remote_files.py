"""list_remote_files uses build_ssh_connection + askpass (no sshpass)."""

import types

import sshpilot.scp_utils as scp_mod


def test_list_remote_files_uses_native_builder(monkeypatch):
    captured = {}

    def _build(ctx):
        captured["remote"] = ctx.remote_command
        captured["native"] = ctx.native_mode
        return types.SimpleNamespace(
            command=["ssh", "-F", "/tmp/cfg", "host", ctx.remote_command],
            env={
                "SSH_ASKPASS": "/tmp/askpass",
                "SSH_ASKPASS_REQUIRE": "prefer",
            },
            password="sekret",
            use_askpass=True,
            use_sshpass=False,
        )

    monkeypatch.setattr(
        "sshpilot.ssh_connection_builder.build_ssh_connection", _build
    )

    def _run(argv, **kwargs):
        captured["argv"] = argv
        captured["env"] = kwargs.get("env") or {}
        captured["timeout"] = kwargs.get("timeout")
        assert "sshpass" not in argv[0]
        stdout = (
            "__SSHPILOT_BEGIN__\n"
            "file.txt\n"
            "dir/\n"
            "__SSHPILOT_STATUS__0\n"
            "__SSHPILOT_END__\n"
        )
        return types.SimpleNamespace(returncode=0, stdout=stdout, stderr="")

    monkeypatch.setattr(scp_mod.subprocess, "run", _run)

    connection = types.SimpleNamespace(
        nickname="host", password="sekret", username="alice"
    )
    entries, err = scp_mod.list_remote_files(connection, "/tmp")
    assert err is None
    assert entries == [("file.txt", False), ("dir", True)]
    assert captured["native"] is True
    assert "ls -1pL" in captured["remote"]
    assert captured["env"].get("SSH_ASKPASS_REQUIRE") == "prefer"
    assert captured["env"].get("SSH_ASKPASS") == "/tmp/askpass"
    # Staged password autofills instantly — short timeout is fine.
    assert captured["timeout"] == 10


def test_list_remote_files_extends_timeout_for_interactive_auth(monkeypatch):
    """No staged secret → askpass may block on a human; don't kill at 10s."""
    monkeypatch.setattr(
        "sshpilot.ssh_connection_builder.build_ssh_connection",
        lambda ctx: types.SimpleNamespace(
            command=["ssh", "host", "true"],
            env={"SSH_ASKPASS": "/a", "SSH_ASKPASS_REQUIRE": "prefer"},
            password=None,
            use_askpass=True,
            use_sshpass=False,
        ),
    )

    captured = {}

    def _run(argv, **kwargs):
        captured["timeout"] = kwargs.get("timeout")
        stdout = (
            "__SSHPILOT_BEGIN__\n"
            "__SSHPILOT_STATUS__0\n"
            "__SSHPILOT_END__\n"
        )
        return types.SimpleNamespace(returncode=0, stdout=stdout, stderr="")

    monkeypatch.setattr(scp_mod.subprocess, "run", _run)

    entries, err = scp_mod.list_remote_files(
        types.SimpleNamespace(nickname="h"), "."
    )
    assert err is None
    assert captured["timeout"] >= 180


def test_summarize_listing_error_strips_log_and_friendlies_auth():
    summ = scp_mod._summarize_listing_error
    # Cancelled prompt: verbose log + auth failure → clean friendly line.
    raw = (
        "debug1: Authenticating to host\n"
        "debug1: Next authentication method: keyboard-interactive\n"
        "root@host: Permission denied (keyboard-interactive).\n"
    )
    msg = summ(raw, "fallback")
    assert "debug" not in msg
    assert "cancel" in msg.lower()

    # Non-auth error survives (minus debug chatter).
    raw2 = "debug1: noise\nls: cannot access '/x': No such file or directory\n"
    assert summ(raw2, "fallback") == "ls: cannot access '/x': No such file or directory"

    # Only debug chatter → fallback.
    assert summ("debug1: only\ndebug2: noise", "fallback") == "fallback"


def test_list_remote_files_forces_askpass_when_resolver_omits_it(monkeypatch):
    monkeypatch.setattr(
        "sshpilot.ssh_connection_builder.build_ssh_connection",
        lambda ctx: types.SimpleNamespace(
            command=["ssh", "host", "true"],
            env={},
            password=None,
            use_askpass=False,
            use_sshpass=False,
        ),
    )
    monkeypatch.setattr(
        "sshpilot.ssh_connection_builder._askpass_env_for_connection",
        lambda *a, **k: {
            "SSH_ASKPASS": "/forced",
            "SSH_ASKPASS_REQUIRE": "prefer",
        },
    )

    captured = {}

    def _run(argv, **kwargs):
        captured["env"] = kwargs.get("env") or {}
        stdout = (
            "__SSHPILOT_BEGIN__\n"
            "__SSHPILOT_STATUS__0\n"
            "__SSHPILOT_END__\n"
        )
        return types.SimpleNamespace(returncode=0, stdout=stdout, stderr="")

    monkeypatch.setattr(scp_mod.subprocess, "run", _run)

    entries, err = scp_mod.list_remote_files(
        types.SimpleNamespace(nickname="h"), "."
    )
    assert err is None
    assert entries == []
    assert captured["env"].get("SSH_ASKPASS") == "/forced"
    assert captured["env"].get("SSH_ASKPASS_REQUIRE") == "prefer"
