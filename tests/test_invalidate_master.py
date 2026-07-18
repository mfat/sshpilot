"""invalidate_master uses the single SSH builder and ssh -O stop."""

import types

import sshpilot.ssh_multiplex as mux


def test_invalidate_master_runs_stop_via_builder(monkeypatch):
    captured = {}

    monkeypatch.setattr(
        "sshpilot.ssh_connection_builder.build_ssh_connection",
        lambda ctx: (
            captured.update(extra_args=ctx.extra_args),
            types.SimpleNamespace(command=["ssh", "-O", "stop", "host"], env={}),
        )[1],
    )

    ran = {}
    monkeypatch.setattr(
        mux.subprocess, "run",
        lambda argv, **kw: ran.update(argv=argv)
        or types.SimpleNamespace(returncode=0),
    )

    connection = types.SimpleNamespace(nickname="host")
    mux.invalidate_master(connection, background=False)

    assert captured["extra_args"][:2] == ["-O", "stop"]
    assert any(str(a).startswith("ControlPath=") for a in captured["extra_args"])
    assert ran["argv"] == ["ssh", "-O", "stop", "host"]
