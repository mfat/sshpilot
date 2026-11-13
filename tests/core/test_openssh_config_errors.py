"""Which lines OpenSSH rejects, read from what OpenSSH itself reports."""

from __future__ import annotations

import shutil

import pytest

from sshpilot.core.ssh_config_effective import (
    openssh_config_errors,
    parse_openssh_config_errors,
)

needs_ssh = pytest.mark.skipif(shutil.which("ssh") is None, reason="needs OpenSSH")


def test_parses_both_spellings_openssh_uses():
    """A bad keyword is reported with a colon after the file, a bad argument
    without one; the closing summary names no line and is not an error."""
    stderr = (
        "/home/me/.ssh/config: line 2: Bad configuration option: bogus\n"
        "/home/me/.ssh/config line 3: Bad port 'abc'.\n"
        "/home/me/.ssh/config: terminating, 2 bad configuration options\n"
    )
    assert parse_openssh_config_errors(stderr) == [
        {
            "file": "/home/me/.ssh/config", "line": 2,
            "diagnostic": "Bad configuration option: bogus",
        },
        {"file": "/home/me/.ssh/config", "line": 3, "diagnostic": "Bad port 'abc'."},
    ]


def test_a_windows_path_keeps_its_drive_colon():
    found = parse_openssh_config_errors(
        "C:\\\\Users\\\\me/.ssh/config: line 7: Bad configuration option: x\n"
    )
    assert found[0]["file"] == "C:\\\\Users\\\\me/.ssh/config"
    assert found[0]["line"] == 7


def test_a_bad_command_line_option_is_not_a_config_error():
    assert parse_openssh_config_errors(
        "command-line: line 0: Bad configuration option: bogus\n"
    ) == []
    assert parse_openssh_config_errors("") == []
    assert parse_openssh_config_errors("ssh: Could not resolve hostname x\n") == []


@needs_ssh
def test_openssh_names_a_line_in_a_block_for_another_host(tmp_path):
    """OpenSSH parses every block whatever host is asked about, so one probe
    finds a mistake under any Host line."""
    config = tmp_path / "config"
    config.write_text(
        "Host *\n    User deploy\n *   IdentityAgent none\n\n"
        "Host web\n    Port abc\n",
        encoding="utf-8",
    )
    found = openssh_config_errors(str(config))
    assert [(item["file"], item["line"]) for item in found] == [
        (str(config), 3), (str(config), 6),
    ]
    assert "Bad configuration option" in found[0]["diagnostic"]


@needs_ssh
def test_a_valid_or_missing_config_has_no_errors(tmp_path):
    config = tmp_path / "config"
    config.write_text("Host web\n    User admin\n", encoding="utf-8")
    assert openssh_config_errors(str(config)) == []
    assert openssh_config_errors(str(tmp_path / "absent")) == []
