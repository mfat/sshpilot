"""Tests for SSH-server-specific transfer concurrency limits."""

from __future__ import annotations

from sshpilot.daemon.sftp_runtime import _argv_with_remote_banner_logging
from sshpilot.sftp.server_limits import (
    is_dropbear_software,
    parse_remote_software_version,
    transfer_concurrency_for_remote_software,
)


def test_parse_remote_software_version_from_openssh_debug():
    text = (
        "debug1: Remote protocol version 2.0, remote software version dropbear\n"
        "debug1: compat_banner: no match: dropbear\n"
    )
    assert parse_remote_software_version(text) == "dropbear"


def test_parse_remote_software_version_openssh_banner():
    text = "debug1: Remote protocol version 2.0, remote software version OpenSSH_9.6\n"
    assert parse_remote_software_version(text) == "OpenSSH_9.6"


def test_parse_remote_software_version_missing():
    assert parse_remote_software_version("debug1: Connecting to example.com\n") is None
    assert parse_remote_software_version("") is None


def test_is_dropbear_software_matches_variants():
    assert is_dropbear_software("dropbear")
    assert is_dropbear_software("dropbear_2024.85")
    assert is_dropbear_software("Dropbear")
    assert not is_dropbear_software("OpenSSH_9.6")
    assert not is_dropbear_software(None)
    assert not is_dropbear_software("")


def test_transfer_concurrency_serializes_dropbear_only():
    assert (
        transfer_concurrency_for_remote_software("dropbear", default=4, dropbear=1) == 1
    )
    assert (
        transfer_concurrency_for_remote_software("OpenSSH_9.6", default=4, dropbear=1)
        == 4
    )
    assert transfer_concurrency_for_remote_software(None, default=4, dropbear=1) == 4


def test_argv_with_remote_banner_logging_injects_debug1():
    assert _argv_with_remote_banner_logging(("ssh", "host", "-s", "sftp")) == (
        "ssh",
        "-o",
        "LogLevel=DEBUG1",
        "host",
        "-s",
        "sftp",
    )


def test_argv_with_remote_banner_logging_skips_when_already_verbose():
    assert _argv_with_remote_banner_logging(("ssh", "-v", "host", "-s", "sftp")) == (
        "ssh",
        "-v",
        "host",
        "-s",
        "sftp",
    )
    assert _argv_with_remote_banner_logging(
        ("ssh", "-o", "LogLevel=ERROR", "host", "-s", "sftp")
    ) == ("ssh", "-o", "LogLevel=ERROR", "host", "-s", "sftp")
