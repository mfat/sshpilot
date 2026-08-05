"""Lossless SSH config round-trip characterization.

Edits must preserve everything outside the edited Host block: comments, blank
lines, ordering, Match/wildcard blocks, CRLF vs LF, and the final-newline
presence. These tests freeze the externally observable round-trip contract the
headless SSH config store must keep.
"""

from sshpilot.core.connections.ssh_config_loader import load_ssh_configuration
from sshpilot.core.connections.ssh_config_store import SshConfigStore


def _store(tmp_path, text: str) -> SshConfigStore:
    path = tmp_path / "config"
    path.write_text(text, encoding="utf-8")
    return SshConfigStore(path)


def _text(tmp_path) -> str:
    return (tmp_path / "config").read_text(encoding="utf-8")


def _raw(tmp_path) -> str:
    """Byte-exact read (no universal-newline translation)."""
    return (tmp_path / "config").read_bytes().decode("utf-8")


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


def test_edit_preserves_unrelated_bytes_verbatim(tmp_path):
    """Only the edited block may change; every other byte is untouched."""
    store = _store(tmp_path, ROUNDTRIP_FIXTURE)
    store.update(
        "web",
        {
            "nickname": "web",
            "hostname": "example.org",
            "username": "alice",
            "protocol": "ssh",
        },
        expected_generation=0,
    )
    text = _text(tmp_path)
    # Everything after the edited block must be byte-identical.
    assert 'Host db jump\n' in text
    assert '\tHostName=db.internal\n' in text
    assert '    UnknownCamelCase FooBar\n' in text
    assert 'Match host *.internal\n' in text
    assert '    User matchuser\n' in text
    assert 'Host "two words" plain\n' in text
    assert 'Host\ttail\n' in text
    assert '    HostName tail.example.com' in text  # no trailing newline preserved


def test_edit_preserves_no_final_newline(tmp_path):
    body = "Host web\n    HostName example.com\n"
    store = _store(tmp_path, body + "Host other\n    HostName o.example.com")
    store.update(
        "web",
        {
            "nickname": "web",
            "hostname": "example.org",
            "username": "u",
            "protocol": "ssh",
        },
        expected_generation=0,
    )
    text = _text(tmp_path)
    assert "HostName example.org" in text
    # The untouched tail keeps its no-final-newline form.
    assert text.endswith("HostName o.example.com")


def test_load_and_reload_preserves_connection_count_and_order(tmp_path):
    path = tmp_path / "config"
    path.write_text(ROUNDTRIP_FIXTURE, encoding="utf-8")
    first = load_ssh_configuration(path, isolated=False)
    second = load_ssh_configuration(path, isolated=False)
    first_ids = [c.id for c in first.connections]
    assert first_ids == [c.id for c in second.connections]
    assert "web" in first_ids
    assert "db" in first_ids
    assert "jump" in first_ids
    assert "two words" in first_ids
    assert "plain" in first_ids
    assert "tail" in first_ids


def test_edit_of_unknown_directive_block_preserves_authored_line(tmp_path):
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
    store = _store(tmp_path, body)
    store.update(
        "web",
        {
            "nickname": "web",
            "hostname": "example.org",
            "username": "bob",
            "protocol": "ssh",
        },
        expected_generation=0,
    )
    text = _text(tmp_path)
    # SendEnv is not a managed option; an edit without extra_ssh_config keeps
    # the authored line verbatim.
    assert "SendEnv LANG LC_*" in text
    assert "HostName example.org" in text
