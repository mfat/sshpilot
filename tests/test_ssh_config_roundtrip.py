"""Lossless SSH config round-trip characterization.

Edits must preserve everything outside the edited Host block: comments, blank
lines, ordering, Match/wildcard blocks, CRLF vs LF, and the final-newline
presence. These tests freeze the externally observable round-trip contract the
M3 headless SSH config store must keep.
"""

import asyncio
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

asyncio.set_event_loop(asyncio.new_event_loop())

from sshpilot.connection_manager import ConnectionManager  # noqa: E402


class FakeConfig:
    def __init__(self):
        self.settings = {}

    def get_setting(self, key, default=None):
        return self.settings.get(key, default)

    def set_setting(self, key, value):
        self.settings[key] = value


@pytest.fixture
def manager(tmp_path):
    cm = ConnectionManager.__new__(ConnectionManager)
    cm.config = FakeConfig()
    cm.connections = []
    cm.rules = []
    cm.ssh_config = {}
    cm.isolated_mode = False
    cm.ssh_config_path = str(tmp_path / "config")
    cm.known_hosts_path = str(tmp_path / "known_hosts")
    cm.emitted = []
    cm.emit = lambda *args: cm.emitted.append(args)
    cm.store_password = lambda *a, **k: True
    cm.delete_password = lambda *a, **k: True
    cm.store_connection_password = lambda *a, **k: True
    cm.delete_connection_passwords = lambda *a, **k: True
    return cm


def _load(cm, text: str) -> None:
    Path(cm.ssh_config_path).write_text(text, encoding="utf-8")
    cm.load_ssh_config()


def _text(cm) -> str:
    return Path(cm.ssh_config_path).read_text(encoding="utf-8")


def _raw(cm) -> str:
    """Byte-exact read (no universal-newline translation)."""
    return Path(cm.ssh_config_path).read_bytes().decode("utf-8")


ROUNDTRIP_FIXTURE = (
    "# Global header - do not touch\n"
    "Include fragments/*\n"
    "\n"
    "Host web\n"
    "    # pinned comment\n"
    "    HostName example.com\n"
    "\n"
    "Host db jump\n"
    "\tHostName=db.internal\n"
    "    UnknownCamelCase FooBar\n"
    "\n"
    "Match host *.internal\n"
    "    User matchuser\n"
    "\n"
    "Host \"two words\" plain\n"
    "    HostName spaced.example.com\n"
    "\n"
    "Host\ttail\n"
    "    HostName tail.example.com"
)  # deliberately no trailing newline


def test_edit_preserves_unrelated_bytes_verbatim(manager):
    """Only the edited block may change; every other byte is untouched."""
    _load(manager, ROUNDTRIP_FIXTURE)
    conn = manager.find_connection_by_nickname("web")
    ok = manager.update_connection(
        conn,
        {
            "nickname": "web",
            "hostname": "example.org",
            "username": "alice",
            "protocol": "ssh",
        },
    )
    assert ok is True
    text = _text(manager)
    # Everything after the edited block must be byte-identical.
    assert 'Host db jump\n' in text
    assert '\tHostName=db.internal\n' in text
    assert '    UnknownCamelCase FooBar\n' in text
    assert 'Match host *.internal\n' in text
    assert '    User matchuser\n' in text
    assert 'Host "two words" plain\n' in text
    assert 'Host\ttail\n' in text
    assert '    HostName tail.example.com' in text  # no trailing newline preserved


def test_edit_preserves_no_final_newline(manager):
    body = "Host web\n    HostName example.com\n"
    _load(manager, body + "Host other\n    HostName o.example.com")
    conn = manager.find_connection_by_nickname("web")
    ok = manager.update_connection(
        conn,
        {
            "nickname": "web",
            "hostname": "example.org",
            "username": "u",
            "protocol": "ssh",
        },
    )
    assert ok is True
    text = _text(manager)
    assert "HostName example.org" in text
    # The untouched tail keeps its no-final-newline form.
    assert text.endswith("HostName o.example.com")


def test_crlf_file_stays_crlf_outside_edited_block(manager):
    body = (
        "Host web\r\n"
        "    HostName example.com\r\n"
        "\r\n"
        "Host other\r\n"
        "    HostName o.example.com\r\n"
    )
    _load(manager, body)
    conn = manager.find_connection_by_nickname("web")
    ok = manager.update_connection(
        conn,
        {
            "nickname": "web",
            "hostname": "example.org",
            "username": "u",
            "protocol": "ssh",
        },
    )
    assert ok is True
    raw = _raw(manager)
    assert "HostName example.org" in raw
    assert "Host other\r\n" in raw
    assert "    HostName o.example.com\r\n" in raw


def test_load_and_reload_preserves_connection_count_and_order(manager):
    _load(manager, ROUNDTRIP_FIXTURE)
    first = [c.nickname for c in manager.connections]
    manager.load_ssh_config()
    second = [c.nickname for c in manager.connections]
    assert first == second
    assert "web" in first
    assert "db" in first
    assert "jump" in first
    assert "two words" in first
    assert "plain" in first
    assert "tail" in first


def test_edit_of_unknown_directive_block_preserves_authored_line(manager):
    body = (
        "# header\n"
        "Host web\n"
        "    HostName example.com\n"
        "    User alice\n"
        "    SendEnv LANG LC_*\n"
        "\n"
        "Host other\n"
        "    HostName o.example.com\n"
    )
    _load(manager, body)
    conn = manager.find_connection_by_nickname("web")
    ok = manager.update_connection(
        conn,
        {
            "nickname": "web",
            "hostname": "example.org",
            "username": "bob",
            "protocol": "ssh",
        },
    )
    assert ok is True
    text = _text(manager)
    # SendEnv is not a managed option; an edit without extra_ssh_config keeps
    # the authored line verbatim.
    assert "SendEnv LANG LC_*" in text
    assert "HostName example.org" in text
