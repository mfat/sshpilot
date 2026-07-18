"""list_remote_files uses build_ssh_connection + askpass (no sshpass)."""

import types

import sshpilot.window as window_mod


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
        assert "sshpass" not in argv[0]
        stdout = (
            "__SSHPILOT_BEGIN__\n"
            "file.txt\n"
            "dir/\n"
            "__SSHPILOT_STATUS__0\n"
            "__SSHPILOT_END__\n"
        )
        return types.SimpleNamespace(returncode=0, stdout=stdout, stderr="")

    monkeypatch.setattr(window_mod.subprocess, "run", _run)

    connection = types.SimpleNamespace(
        nickname="host", password="sekret", username="alice"
    )
    entries, err = window_mod.list_remote_files(connection, "/tmp")
    assert err is None
    assert entries == [("file.txt", False), ("dir", True)]
    assert captured["native"] is True
    assert "ls -1pL" in captured["remote"]
    assert captured["env"].get("SSH_ASKPASS_REQUIRE") == "prefer"
    assert captured["env"].get("SSH_ASKPASS") == "/tmp/askpass"


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

    monkeypatch.setattr(window_mod.subprocess, "run", _run)

    entries, err = window_mod.list_remote_files(
        types.SimpleNamespace(nickname="h"), "."
    )
    assert err is None
    assert entries == []
    assert captured["env"].get("SSH_ASKPASS") == "/forced"
    assert captured["env"].get("SSH_ASKPASS_REQUIRE") == "prefer"
