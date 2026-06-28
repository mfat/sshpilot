"""Unit tests for the authorized_keys parser."""

from __future__ import annotations


from sshpilot.authorized_keys_parser import (
    AuthorizedKeyEntry,
    compute_fingerprint,
    parse_file,
    serialize,
)


KEY_BLOB = (
    "AAAAC3NzaC1lZDI1NTE5AAAAIK1eKZmcG3zP7Q9XzYx0sLh9G6mP6Q3OwPgkqJ"
    "x6r1Pq"
)


def _entries(items):
    return [it for it in items if isinstance(it, AuthorizedKeyEntry)]


def test_parses_plain_line():
    text = f"ssh-ed25519 {KEY_BLOB} alice@host\n"
    items = parse_file(text)
    entries = _entries(items)
    assert len(entries) == 1
    e = entries[0]
    assert e.keytype == "ssh-ed25519"
    assert e.key_b64 == KEY_BLOB
    assert e.comment == "alice@host"
    assert e.options == []
    assert e.unknown_options == []
    assert not e.disabled


def test_roundtrip_clean_is_byte_identical():
    text = (
        "# header comment\n"
        "\n"
        f'command="/usr/local/bin/run.sh",from="10.0.0.0/8,!10.0.0.5",restrict ssh-ed25519 {KEY_BLOB} alice\n'
        f"ssh-rsa {KEY_BLOB} bob with spaces in comment\n"
        f"# ssh-ed25519 {KEY_BLOB} disabled-key\n"
    )
    items = parse_file(text)
    assert serialize(items) == text


def test_unknown_option_preserved_verbatim():
    text = (
        f'weird-future-option="x,y",command="/bin/echo" ssh-ed25519 {KEY_BLOB} bob\n'
    )
    items = parse_file(text)
    entries = _entries(items)
    assert len(entries) == 1
    e = entries[0]
    assert any(name == "command" for name, _ in e.options)
    assert e.unknown_options == ['weird-future-option="x,y"']
    # Even after mutating something, the unknown option must still be emitted.
    e.set_value("command", "/bin/true")
    out = serialize(items)
    assert 'weird-future-option="x,y"' in out
    assert 'command="/bin/true"' in out


def test_repeatable_options():
    text = (
        f'permitopen="host1:22",permitopen="host2:80",environment="FOO=bar" '
        f"ssh-ed25519 {KEY_BLOB} repeatable\n"
    )
    items = parse_file(text)
    e = _entries(items)[0]
    permits = e.get_options("permitopen")
    assert permits == ["host1:22", "host2:80"]
    assert e.get_options("environment") == ["FOO=bar"]


def test_disabled_entry_roundtrips():
    text = f"# ssh-ed25519 {KEY_BLOB} old-key\n"
    items = parse_file(text)
    entries = _entries(items)
    assert len(entries) == 1
    assert entries[0].disabled is True
    assert serialize(items) == text


def test_pure_comment_passthrough():
    text = "# just a note about this file\n"
    items = parse_file(text)
    # No entries; the comment is passthrough.
    assert _entries(items) == []
    assert serialize(items) == text


def test_quoted_values_with_escapes():
    text = (
        f'command="echo \\"hi\\" && true" ssh-ed25519 {KEY_BLOB} esc\n'
    )
    items = parse_file(text)
    e = _entries(items)[0]
    assert e.get_option("command") == 'echo "hi" && true'
    # Round-trip the mutation re-quotes it.
    e.set_value("command", 'echo "hi" && true')
    out = serialize(items)
    assert '\\"hi\\"' in out


def test_fingerprint_format():
    fp = compute_fingerprint("ssh-ed25519", KEY_BLOB)
    assert fp.startswith("SHA256:")
    # base64 sha256 (no padding) is 43 chars.
    assert len(fp) == len("SHA256:") + 43


def test_mutation_marks_dirty_and_serializes_change():
    text = f"ssh-ed25519 {KEY_BLOB} alice\n"
    items = parse_file(text)
    e = _entries(items)[0]
    e.set_flag("restrict", True)
    out = serialize(items)
    assert out.startswith("restrict ssh-ed25519 ")


def test_blank_lines_preserved():
    text = f"\n\nssh-ed25519 {KEY_BLOB} a\n\n"
    items = parse_file(text)
    assert serialize(items) == text


def test_cert_keytype_recognized():
    cert = "ssh-ed25519-cert-v01@openssh.com"
    text = f"{cert} {KEY_BLOB} certuser\n"
    items = parse_file(text)
    e = _entries(items)[0]
    assert e.keytype == cert


def test_flag_option_no_equals():
    text = f"restrict,cert-authority ssh-ed25519 {KEY_BLOB} ca\n"
    items = parse_file(text)
    e = _entries(items)[0]
    assert ("restrict", True) in e.options
    assert ("cert-authority", True) in e.options


def test_false_flag_is_not_emitted():
    """A flag option stored with value False must NOT round-trip as the
    bare name (which would silently re-enable it). It should be dropped."""
    text = f"ssh-ed25519 {KEY_BLOB} alice\n"
    items = parse_file(text)
    e = _entries(items)[0]
    # Inject an inconsistent False flag and ensure serialisation drops it.
    e.options = [("restrict", False), ("cert-authority", False)]
    e.mark_dirty()
    out = serialize(items)
    assert "restrict" not in out
    assert "cert-authority" not in out
    # The key line is still emitted.
    assert "ssh-ed25519" in out
    # And it's well-formed (no stray commas / leading space).
    line = out.strip().splitlines()[0]
    assert line.startswith("ssh-ed25519 ")
