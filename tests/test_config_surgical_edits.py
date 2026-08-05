"""Surgical-edit guarantees for the SSH-config document model.

An edit/remove/split touches ONLY the target block's span, byte-for-byte. A
block's span runs from its ``Host`` header up to (not including) the next
``Host``/``Match``/``Include`` header — so trailing comments/blank lines inside
that span belong to the block. In-block comments, trailing comments, and the
authored casing of unknown directives survive edits. Repeated unknown
directives (SendEnv/SetEnv) and CRLF endings are preserved without
double-conversion, and a missing final newline never glues lines together.
"""

from sshpilot.core.connections.ssh_config_loader import load_ssh_configuration
from sshpilot.core.connections.ssh_config_store import SshConfigStore


ROOT = (
    "# Global header - do not touch\n"
    "Host web\n"
    "    HostName example.com\n"
    "    User alice\n"
    "\n"
    "Host db jump\n"
    "\tHostName db.internal\n"
    "    UnknownCamelCase FooBar\n"
    "\n"
    "Match host *.internal\n"
    "    User matchuser\n"
    "\n"
    "Host\ttail\n"
    "    HostName tail.example.com\n"
)
# Everything from the second block onward — must survive edits to 'web' verbatim.
SUFFIX = ROOT[ROOT.index("Host db jump"):]


def _store(tmp_path, text: str = ROOT) -> SshConfigStore:
    path = tmp_path / "config"
    path.write_text(text, encoding="utf-8")
    return SshConfigStore(path)


def _text(tmp_path) -> str:
    return (tmp_path / "config").read_text(encoding="utf-8")


# --- Edit / remove / split span isolation -----------------------------------


def test_edit_preserves_everything_outside_the_block_span(tmp_path):
    store = _store(tmp_path)
    suffix = _text(tmp_path)[_text(tmp_path).index("Host db jump"):]
    store.update(
        "web",
        {"nickname": "web", "hostname": "example.com", "username": "bob",
         "protocol": "ssh"},
        expected_generation=0,
    )
    text = _text(tmp_path)
    assert text.startswith("# Global header - do not touch\n")
    assert text.endswith(suffix)
    assert "    User bob\n" in text
    assert "alice" not in text


def test_remove_preserves_everything_outside_the_block_span(tmp_path):
    store = _store(tmp_path)
    suffix = _text(tmp_path)[_text(tmp_path).index("Host db jump"):]
    store.delete("web")
    assert _text(tmp_path) == "# Global header - do not touch\n" + suffix


def test_edit_last_block_preserves_leading_content(tmp_path):
    store = _store(tmp_path)
    prefix = _text(tmp_path)[: _text(tmp_path).index("Host\ttail")]
    store.update(
        "tail",
        {"nickname": "tail", "hostname": "tail.example.com", "username": "eve",
         "protocol": "ssh"},
        expected_generation=0,
    )
    text = _text(tmp_path)
    assert text.startswith(prefix)
    assert "    User eve\n" in text


def test_split_keeps_sibling_alias_block_body(tmp_path):
    """Editing 'db' out of 'Host db jump' keeps jump's body verbatim and
    appends a dedicated db block at the end."""
    store = _store(tmp_path)
    store.split(
        "db",
        "db",
        {"nickname": "db", "hostname": "db.internal", "username": "carol",
         "protocol": "ssh"},
        expected_generation=0,
    )
    text = _text(tmp_path)
    assert "Host jump\n" in text
    jump_block = text[text.index("Host jump\n"):text.index("\nMatch host")]
    assert "\tHostName db.internal\n    UnknownCamelCase FooBar\n" in jump_block
    assert text.rstrip().endswith("    User carol")  # new block appended last
    assert "# Global header - do not touch\n" in text
    assert "Match host *.internal\n    User matchuser\n" in text


def test_rename_replaces_only_the_target_block(tmp_path):
    store = _store(tmp_path)
    suffix = _text(tmp_path)[_text(tmp_path).index("Host db jump"):]
    store.update(
        "web",
        {"nickname": "web2", "hostname": "example.com", "username": "alice",
         "protocol": "ssh"},
        expected_generation=0,
    )
    text = _text(tmp_path)
    assert "Host web2\n" in text
    assert "Host web\n" not in text
    assert text.endswith(suffix)


def test_repeated_blocks_for_same_host_collapse_on_edit(tmp_path):
    """Duplicate 'Host web' stanzas merge into one rewritten block on edit —
    mirrors ssh's merge semantics for repeated Host blocks."""
    doubled = ROOT + "\nHost web\n    Port 2222\n"
    store = _store(tmp_path, doubled)
    store.update(
        "web",
        {"nickname": "web", "hostname": "example.com", "username": "bob",
         "protocol": "ssh"},
        expected_generation=0,
    )
    assert _text(tmp_path).count("Host web\n") == 1


def test_edit_included_host_leaves_root_untouched(tmp_path):
    (tmp_path / "fragments").mkdir()
    frag = tmp_path / "fragments" / "extra"
    frag.write_text("Host frag\n    HostName frag.example.com\n    User alice\n")
    root_text = "Include fragments/extra\n\nHost web\n    HostName example.com\n"
    _store(tmp_path, root_text)
    migrated_root = _text(tmp_path)
    loaded = load_ssh_configuration(tmp_path / "config", isolated=False)
    conn = next(c for c in loaded.connections if c.id == "frag")
    assert conn is not None and str(conn.source) == str(frag)
    SshConfigStore(frag).update(
        "frag",
        {"nickname": "frag", "hostname": "frag.example.com", "username": "bob",
         "protocol": "ssh"},
        expected_generation=conn.generation,
    )
    assert _text(tmp_path) == migrated_root
    assert "    User bob\n" in frag.read_text()


def test_edit_last_block_without_trailing_newline_does_not_glue(tmp_path):
    """A file whose last block has no trailing newline: appending managed
    directives must start a new line, not concatenate onto the last authored
    line (which would silently drop the edit and duplicate on re-save)."""
    text = "Host web\n    UnknownCamelCase foo   # why"  # no trailing "\n"
    store = _store(tmp_path, text)
    payload = {"nickname": "web", "hostname": "new.example.com", "username": "bob",
               "protocol": "ssh"}
    store.update("web", payload, expected_generation=0)
    once = _text(tmp_path)
    assert once.startswith("Host web\n")
    assert "    UnknownCamelCase foo   # why\n" in once
    assert "    HostName new.example.com\n" in once
    assert "    User bob\n" in once
    # ...and the edit is idempotent (no duplicate HostName on a second save).
    store.update("web", payload, expected_generation=1)
    assert _text(tmp_path) == once


# --- Surgical-merge guarantees (delivered by the document model) -----------


def test_comment_inside_edited_block_survives(tmp_path):
    text = (
        "Host web\n"
        "    # pinned to the old DC on purpose\n"
        "    HostName example.com\n"
        "    User alice\n"
    )
    store = _store(tmp_path, text)
    store.update(
        "web",
        {"nickname": "web", "hostname": "example.com", "username": "bob",
         "protocol": "ssh"},
        expected_generation=0,
    )
    assert "# pinned to the old DC on purpose" in _text(tmp_path)


def test_trailing_comment_after_edited_block_survives(tmp_path):
    text = (
        "Host web\n"
        "    HostName example.com\n"
        "\n"
        "# db cluster below\n"
        "Host db\n"
        "    HostName db.internal\n"
    )
    store = _store(tmp_path, text)
    store.update(
        "web",
        {"nickname": "web", "hostname": "example.com", "username": "bob",
         "protocol": "ssh"},
        expected_generation=0,
    )
    assert "# db cluster below" in _text(tmp_path)


def test_crlf_config_fully_preserved_on_edit(tmp_path):
    """A CRLF config stays CRLF everywhere after an edit — untouched blocks
    byte-for-byte, generated lines converted to the document's style."""
    text = (
        "Host web\r\n"
        "    HostName example.com\r\n"
        "    User alice\r\n"
        "\r\n"
        "Host db\r\n"
        "    HostName db.internal\r\n"
    )
    path = tmp_path / "config"
    path.write_bytes(text.encode())
    store = SshConfigStore(path)
    migrated = path.read_bytes().decode()
    db_block = migrated[migrated.index("Host db\r\n"):]
    store.update(
        "web",
        {"nickname": "web", "hostname": "example.com", "username": "bob",
         "protocol": "ssh"},
        expected_generation=0,
    )
    raw = path.read_bytes().decode()
    assert "\n" not in raw.replace("\r\n", "")  # every line ending is CRLF
    assert "    User bob\r\n" in raw
    assert raw.endswith(db_block)


def test_crlf_edit_preserving_comment_and_unknown_directive(tmp_path):
    """Preserved lines already carry CRLF; merging them with generated LF
    lines must not double-convert them to CR CR LF."""
    text = (
        "Host web\r\n"
        "    # keep\r\n"
        "    HostName example.com\r\n"
        "    SendEnv FOO\r\n"
    )
    path = tmp_path / "config"
    path.write_bytes(text.encode())
    store = SshConfigStore(path)
    loaded = load_ssh_configuration(path, isolated=False)
    data = next(c for c in loaded.connections if c.id == "web").data
    store.update(
        "web",
        {"nickname": "web", "hostname": "example.com", "username": "bob",
         "protocol": "ssh", "extra_ssh_config": data.get("extra_ssh_config") or ""},
        expected_generation=0,
    )
    raw = path.read_bytes().decode()
    assert "\r\r" not in raw
    assert "\n" not in raw.replace("\r\n", "")  # every ending is a single CRLF
    assert "    # keep\r\n" in raw
    assert "    SendEnv FOO\r\n" in raw
    assert "    User bob\r\n" in raw


def test_crlf_remove_keeps_other_blocks_byte_identical(tmp_path):
    text = (
        "Host web\r\n"
        "    HostName example.com\r\n"
        "Host db\r\n"
        "    HostName db.internal\r\n"
    )
    path = tmp_path / "config"
    path.write_bytes(text.encode())
    store = SshConfigStore(path)
    db_block = path.read_bytes()[path.read_bytes().index(b"Host db\r\n"):]
    store.delete("web")
    assert path.read_bytes() == db_block


def test_missing_final_newline_preserved_when_other_block_edited(tmp_path):
    text = (
        "Host web\n"
        "    HostName example.com\n"
        "\n"
        "Host tail\n"
        "    HostName tail.example.com"  # no final newline
    )
    store = _store(tmp_path, text)
    store.update(
        "web",
        {"nickname": "web", "hostname": "example.com", "username": "bob",
         "protocol": "ssh"},
        expected_generation=0,
    )
    saved = _text(tmp_path)
    assert saved.endswith("    HostName tail.example.com")
    assert "    User bob\n" in saved


def test_repeated_unknown_directives_survive_edit(tmp_path):
    """SendEnv/SetEnv legitimately repeat; every authored occurrence must be
    parsed into extra_ssh_config and survive a dialog-style edit."""
    text = (
        "Host web\n"
        "    HostName example.com\n"
        "    SendEnv FOO\n"
        "    SendEnv BAR\n"
        "    SetEnv A=1\n"
        "    SetEnv B=2\n"
    )
    store = _store(tmp_path, text)
    data = load_ssh_configuration(tmp_path / "config", isolated=False).connections[0].data
    extras = data.get("extra_ssh_config") or ""
    assert "sendenv FOO" in extras and "sendenv BAR" in extras
    assert "setenv A=1" in extras and "setenv B=2" in extras

    store.update(
        "web",
        {"nickname": "web", "hostname": "example.com", "username": "u",
         "protocol": "ssh", "extra_ssh_config": extras},
        expected_generation=0,
    )
    saved = _text(tmp_path)
    for line in ("    SendEnv FOO\n", "    SendEnv BAR\n",
                 "    SetEnv A=1\n", "    SetEnv B=2\n"):
        assert line in saved


def test_identical_repeated_unknown_directives_survive_edit(tmp_path):
    text = (
        "Host web\n"
        "    HostName example.com\n"
        "    SendEnv FOO\n"
        "    SendEnv FOO\n"
    )
    store = _store(tmp_path, text)
    data = load_ssh_configuration(tmp_path / "config", isolated=False).connections[0].data
    extras = data.get("extra_ssh_config") or ""
    store.update(
        "web",
        {"nickname": "web", "hostname": "example.com", "username": "u",
         "protocol": "ssh", "extra_ssh_config": extras},
        expected_generation=0,
    )
    assert _text(tmp_path).count("    SendEnv FOO\n") == 2


def test_unknown_directive_casing_survives_edit(tmp_path):
    text = (
        "Host web\n"
        "    HostName example.com\n"
        "    ServerAliveInterval 60\n"
    )
    store = _store(tmp_path, text)
    data = load_ssh_configuration(tmp_path / "config", isolated=False).connections[0].data
    extras = data.get("extra_ssh_config") or ""
    # A dialog-style payload carries the parsed extras back (lowercased today).
    store.update(
        "web",
        {"nickname": "web", "hostname": "example.com", "username": "u",
         "protocol": "ssh", "extra_ssh_config": extras},
        expected_generation=0,
    )
    assert "ServerAliveInterval 60" in _text(tmp_path)
