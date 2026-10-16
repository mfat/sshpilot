"""Tests for stripping config-static SSH port forwards for daemon launches."""

from __future__ import annotations

import os
import subprocess
import sys

import pytest
from pathlib import Path

from sshpilot.core.ssh_config_forward_strip import (
    argv_config_file,
    argv_with_config_file,
    materialize_ssh_config_without_port_forwards,
)


def test_materialize_strips_dynamic_and_local_forwards(tmp_path):
    source = tmp_path / "config"
    source.write_text(
        "Host US\n"
        "  HostName 127.0.0.1\n"
        "  User root\n"
        "  DynamicForward localhost:7000\n"
        "  LocalForward 8080 127.0.0.1:8080\n"
        "  IdentityFile ~/.ssh/id_ed25519\n",
        encoding="utf-8",
    )
    out = materialize_ssh_config_without_port_forwards(
        str(source), destination_dir=str(tmp_path / "stripped")
    )
    text = Path(out).read_text(encoding="utf-8").lower()
    assert "dynamicforward" not in text
    assert "localforward" not in text
    assert "hostname 127.0.0.1" in text
    assert "user root" in text


def test_materialize_rewrites_include_and_strips_nested(tmp_path):
    included = tmp_path / "extra.conf"
    included.write_text(
        "Host jump\n  HostName 10.0.0.1\n  DynamicForward 1080\n",
        encoding="utf-8",
    )
    source = tmp_path / "config"
    source.write_text(
        f"Include {included.name}\n"
        "Host US\n"
        "  HostName 127.0.0.1\n"
        "  RemoteForward 9090 localhost:9090\n",
        encoding="utf-8",
    )
    out = materialize_ssh_config_without_port_forwards(
        str(source), destination_dir=str(tmp_path / "tree")
    )
    root_text = Path(out).read_text(encoding="utf-8")
    assert "RemoteForward" not in root_text
    assert "Include" in root_text
    # Nested file must lose DynamicForward but keep HostName.
    nested_files = [p for p in Path(out).parent.rglob("*") if p.name.endswith("extra.conf")]
    assert nested_files
    nested = nested_files[0].read_text(encoding="utf-8").lower()
    assert "dynamicforward" not in nested
    assert "hostname 10.0.0.1" in nested


def test_stripped_config_keeps_cli_localforward_drops_config_dynamic(tmp_path):
    source = tmp_path / "config"
    source.write_text(
        "Host testhost\n"
        "  HostName 127.0.0.1\n"
        "  User nobody\n"
        "  DynamicForward localhost:17999\n",
        encoding="utf-8",
    )
    stripped = materialize_ssh_config_without_port_forwards(
        str(source), destination_dir=str(tmp_path / "out")
    )
    result = subprocess.run(
        [
            "ssh",
            "-F",
            stripped,
            "-L",
            "127.0.0.1:18001:127.0.0.1:9",
            "-G",
            "testhost",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    lines = {
        line.split(" ", 1)[0]: line
        for line in result.stdout.splitlines()
        if line.startswith(("localforward ", "dynamicforward "))
    }
    assert "dynamicforward" not in lines
    assert "localforward" in lines
    assert "18001" in lines["localforward"]


def test_materialize_keeps_a_lone_cr_inside_its_line(tmp_path):
    # OpenSSH reads this as one comment; a copy with LF there would run it.
    content = b"Host US\n  HostName 127.0.0.1\n  # note\rProxyCommand touch /tmp/x\n"
    source = tmp_path / "config"
    source.write_bytes(content)
    out = materialize_ssh_config_without_port_forwards(
        str(source), destination_dir=str(tmp_path / "stripped")
    )
    # Windows writes the copy with CRLF; the lone CR must stay in its line.
    assert Path(out).read_bytes().replace(b"\r\n", b"\n") == content


def test_argv_config_helpers():
    assert argv_config_file(["ssh", "-F", "/tmp/a", "host"]) == "/tmp/a"
    assert argv_config_file(["ssh", "-F/tmp/b", "host"]) == "/tmp/b"
    assert argv_config_file(["ssh", "host"]) is None
    assert argv_with_config_file(["ssh", "-p", "22", "host"], "/x") == (
        "ssh",
        "-F",
        "/x",
        "-p",
        "22",
        "host",
    )
    assert argv_with_config_file(["ssh", "-F", "/old", "host"], "/new") == (
        "ssh",
        "-F",
        "/new",
        "host",
    )


@pytest.mark.skipif(sys.platform != "win32", reason="OpenSSH for Windows ACL check")
def test_windows_openssh_accepts_the_materialized_includes(tmp_path):
    # Python 3.13+ mkdtemp grants OWNER RIGHTS, which OpenSSH for Windows
    # refuses on an included config ("Bad owner or permissions", exit 255),
    # so every daemon forward from a config with an Include failed.
    import shutil
    import subprocess

    ssh = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "OpenSSH", "ssh.exe")
    if not os.path.isfile(ssh):
        pytest.skip("OpenSSH for Windows is not installed")
    from sshpilot.platform.windows.acl import restrict_directory_to_current_user

    source = tmp_path / "src"
    source.mkdir()
    restrict_directory_to_current_user(source)
    inner = source / "inner.conf"
    inner.write_text("Host probe\n  HostName 192.0.2.1\n  LocalForward 1234 localhost:22\n")
    outer = source / "config"
    outer.write_text("Include " + str(inner).replace(os.sep, "/") + "\n")

    stripped = materialize_ssh_config_without_port_forwards(str(outer))
    try:
        result = subprocess.run([ssh, "-G", "-F", stripped, "probe"], capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stderr
        lines = result.stdout.splitlines()
        assert "hostname 192.0.2.1" in lines
        assert not any(line.startswith("localforward") for line in lines)
    finally:
        shutil.rmtree(os.path.dirname(stripped), ignore_errors=True)
