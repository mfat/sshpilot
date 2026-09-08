"""SSH ProcessSpec construction tests."""
from __future__ import annotations

import pytest

from sshpilot.core.errors import CoreError
from sshpilot.core.ssh import (
    AuthMethod,
    ForwardSpec,
    HostKeyMode,
    LaunchMode,
    SSHLaunchRequest,
    build_ssh_process_spec,
)


def test_password_and_pubkey_and_identities():
    pw = build_ssh_process_spec(
        SSHLaunchRequest(
            destination="demo",
            username="alice",
            port=2222,
            auth_method=AuthMethod.PASSWORD,
            askpass_required=True,
        )
    )
    assert pw.argv == ("ssh", "-p", "2222", "-l", "alice", "demo")
    assert pw.env.get("SSH_ASKPASS_REQUIRE") == "prefer"

    key = build_ssh_process_spec(
        SSHLaunchRequest(
            destination="demo",
            identity_files=["/a", "/b"],
            certificate_file="/c.crt",
            auth_method=AuthMethod.PUBLIC_KEY,
        )
    )
    assert key.argv == (
        "ssh",
        "-i",
        "/a",
        "-i",
        "/b",
        "-o",
        "CertificateFile=/c.crt",
        "demo",
    )


def test_proxy_jump_agent_forwards_hostkey():
    spec = build_ssh_process_spec(
        SSHLaunchRequest(
            destination="target",
            proxy_jump=["jump1", "jump2"],
            agent_forwarding=True,
            forwards=[
                ForwardSpec("local", "8080", "127.0.0.1:80"),
                ForwardSpec("remote", "9090", "127.0.0.1:90"),
                ForwardSpec("dynamic", "1080"),
            ],
            host_key_mode=HostKeyMode.YES,
            compression=True,
            keepalive_interval=30,
            keepalive_count_max=3,
            batch_mode=True,
            remote_command="uptime",
        )
    )
    assert spec.argv == (
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        "ServerAliveInterval=30",
        "-o",
        "ServerAliveCountMax=3",
        "-C",
        "-J",
        "jump1,jump2",
        "-A",
        "-L",
        "8080:127.0.0.1:80",
        "-R",
        "9090:127.0.0.1:90",
        "-D",
        "1080",
        "target",
        "uptime",
    )


def test_local_command_and_contradictions():
    spec = build_ssh_process_spec(
        SSHLaunchRequest(
            destination="demo",
            local_command="echo hi",
            config_file="/tmp/cfg",
        )
    )
    assert spec.argv[:4] == ("ssh", "-F", "/tmp/cfg", "-o")
    with pytest.raises(CoreError):
        build_ssh_process_spec(
            SSHLaunchRequest(
                destination="demo",
                proxy_jump=["j"],
                proxy_command="nc %h %p",
            )
        )
    with pytest.raises(CoreError):
        build_ssh_process_spec(
            SSHLaunchRequest(destination="demo", batch_mode=True, askpass_required=True)
        )
    with pytest.raises(CoreError):
        build_ssh_process_spec(
            SSHLaunchRequest(
                destination="demo",
                local_command="true",
                launch_mode=LaunchMode.SFTP,
            )
        )


def test_force_tty_adds_dash_t_before_destination():
    spec = build_ssh_process_spec(
        SSHLaunchRequest(
            destination="demo",
            remote_command="docker exec -it web sh",
            force_tty=True,
        )
    )
    assert spec.argv == ("ssh", "-t", "demo", "docker exec -it web sh")

    plain = build_ssh_process_spec(
        SSHLaunchRequest(
            destination="demo",
            remote_command="docker exec -it web sh",
            force_tty=False,
        )
    )
    assert plain.argv == ("ssh", "demo", "docker exec -it web sh")


def test_scp_uses_capital_p_port_and_user_option_not_bandwidth_limit():
    """scp(1): ``-P`` is port, ``-p`` preserves times, ``-l`` is Kbit/s."""

    spec = build_ssh_process_spec(
        SSHLaunchRequest(
            destination="alice@host:/tmp",
            executable="scp",
            username="alice",
            port=2222,
            launch_mode=LaunchMode.SCP,
        )
    )
    assert spec.argv == (
        "scp",
        "-P",
        "2222",
        "-o",
        "User=alice",
        "alice@host:/tmp",
    )
    assert "-l" not in spec.argv
    assert "-p" not in spec.argv


def test_scp_binary_keeps_safe_flags_when_launch_mode_is_batch():
    """Flag semantics follow the scp binary, not LaunchMode alone.

    interaction_policy ``none`` used to overwrite LaunchMode.SCP with BATCH
    while executable stayed scp, which revived ``-p``/``-l`` misparse.
    """

    spec = build_ssh_process_spec(
        SSHLaunchRequest(
            destination="alice@host:/tmp",
            executable="/usr/bin/scp",
            username="alice",
            port=2222,
            launch_mode=LaunchMode.BATCH,
            batch_mode=True,
        )
    )
    assert spec.argv[0] == "/usr/bin/scp"
    assert "-P" in spec.argv and spec.argv[spec.argv.index("-P") + 1] == "2222"
    assert "User=alice" in spec.argv
    assert "-l" not in spec.argv
    assert "-p" not in spec.argv
    assert "BatchMode=yes" in spec.argv


def test_scp_preference_overrides_follow_extra_options_before_destination():
    """Preference defaults trail extra_options (ssh first-wins); destination last.

    Only scp *flags* belong in ``extra_options``. Path operands must be inserted
    after this prepared argv (see NativeScpBackend.build_argv): a local path in
    ``extra_options`` would land before ``-o``/``-v`` and OpenSSH would treat
    those flags as more source filenames.
    """

    spec = build_ssh_process_spec(
        SSHLaunchRequest(
            destination="host:/dst",
            executable="scp",
            launch_mode=LaunchMode.SCP,
            extra_options=["-r"],
            ssh_overrides=["-o", "ServerAliveInterval=25", "-v"],
        )
    )
    assert spec.argv == (
        "scp",
        "-r",
        "-o",
        "ServerAliveInterval=25",
        "-v",
        "host:/dst",
    )


def test_scp_path_operand_in_extra_options_lands_before_preference_flags():
    """Pin the footgun that made Preference ``-v``/``-o`` look like sources.

    Production must not put transfer paths in ``extra_options``; this documents
    why (overrides intentionally follow extra_options for ssh defaults).
    """

    broken = build_ssh_process_spec(
        SSHLaunchRequest(
            destination="host:/dst",
            executable="scp",
            launch_mode=LaunchMode.SCP,
            extra_options=["/tmp/payload"],
            ssh_overrides=["-o", "ServerAliveInterval=25", "-v"],
        )
    )
    payload_i = broken.argv.index("/tmp/payload")
    assert broken.argv.index("-v") > payload_i
    assert broken.argv.index("ServerAliveInterval=25") > payload_i


def test_scp_inserting_sources_before_destination_keeps_overrides_as_options():
    """The transfer argv shape after NativeScpBackend.build_argv."""

    prepared = build_ssh_process_spec(
        SSHLaunchRequest(
            destination="alice@host:/remote/drop",
            executable="scp",
            launch_mode=LaunchMode.SCP,
            extra_options=["-r"],
            ssh_overrides=["-o", "ServerAliveInterval=25", "-v"],
        )
    ).argv
    sources = ("/tmp/payload", "/tmp/other")
    argv = (*prepared[:-1], *sources, prepared[-1])
    first_source = argv.index("/tmp/payload")
    assert argv.index("-r") < first_source
    assert argv.index("-v") < first_source
    assert argv.index("ServerAliveInterval=25") < first_source
    assert argv[-1] == "alice@host:/remote/drop"
    assert argv[first_source : first_source + 2] == sources


def test_gi_blocked_import():
    import subprocess
    import sys
    from pathlib import Path

    src = Path(__file__).resolve().parents[2] / "src"
    script = (
        "import builtins, importlib, sys\n"
        f"sys.path.insert(0, {str(src)!r})\n"
        "real = builtins.__import__\n"
        "builtins.__import__ = lambda name, *a, **k: (_ for _ in ()).throw(ImportError('blocked')) "
        "if name == 'gi' or name.startswith('gi.') else real(name, *a, **k)\n"
        "importlib.import_module('sshpilot.core.ssh')\n"
        "print('OK')\n"
    )
    proc = subprocess.run([sys.executable, "-I", "-c", script], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    assert "OK" in proc.stdout
