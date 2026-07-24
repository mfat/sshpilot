"""Step-1 characterization for the SSH-config document-model refactor.

Two families:

- FREEZE tests pin today's invariant: an edit/remove/split touches ONLY the
  target block's span, byte-for-byte. A block's span runs from its ``Host``
  header up to (not including) the next ``Host``/``Match``/``Include`` header —
  so trailing comments/blank lines inside that span belong to the block today.
- Surgical-merge tests assert what the document model delivers: in-block
  comments, trailing comments, and authored casing of unknown directives
  survive edits.
"""

import asyncio
import types


from sshpilot.connection_manager import ConnectionManager

asyncio.set_event_loop(asyncio.new_event_loop())


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


def make_cm(tmp_path):
    cm = ConnectionManager.__new__(ConnectionManager)
    cm.config = types.SimpleNamespace(get_setting=lambda *a, **k: [])
    cm.connections = []
    cm.rules = []
    cm.ssh_config = {}
    cm.isolated_mode = False
    cm.ssh_config_path = str(tmp_path / "config")
    cm.known_hosts_path = str(tmp_path / "known_hosts")
    cm.emit = lambda *args: None
    return cm


def loaded_cm(tmp_path, text=ROOT):
    (tmp_path / "config").write_text(text)
    cm = make_cm(tmp_path)
    cm.load_ssh_config()
    return cm


# --- FREEZE: current behavior ---------------------------------------------


def test_edit_preserves_everything_outside_the_block_span(tmp_path):
    cm = loaded_cm(tmp_path)
    conn = cm.find_connection_by_nickname("web")
    cm.update_ssh_config_file(
        conn, {"nickname": "web", "hostname": "example.com", "username": "bob"}, "web"
    )
    text = (tmp_path / "config").read_text()
    assert text.startswith("# Global header - do not touch\n")
    assert text.endswith(SUFFIX)  # tabs, =, casing, Match block: all verbatim
    assert "    User bob\n" in text
    assert "alice" not in text


def test_remove_preserves_everything_outside_the_block_span(tmp_path):
    cm = loaded_cm(tmp_path)
    removed = cm.remove_ssh_config_entry("web")
    assert removed is True
    text = (tmp_path / "config").read_text()
    assert text == "# Global header - do not touch\n" + SUFFIX


def test_edit_last_block_preserves_leading_content(tmp_path):
    cm = loaded_cm(tmp_path)
    conn = cm.find_connection_by_nickname("tail")
    cm.update_ssh_config_file(
        conn,
        {"nickname": "tail", "hostname": "tail.example.com", "username": "eve"},
        "tail",
    )
    text = (tmp_path / "config").read_text()
    prefix = ROOT[: ROOT.index("Host\ttail")]
    assert text.startswith(prefix)
    assert "    User eve\n" in text


def test_split_keeps_sibling_alias_block_body(tmp_path):
    """Editing 'db' out of 'Host db jump' keeps jump's body verbatim and
    appends a dedicated db block at the end."""
    cm = loaded_cm(tmp_path)
    ok = cm._split_host_block(
        "db",
        {"nickname": "db", "hostname": "db.internal", "username": "carol"},
        str(tmp_path / "config"),
    )
    assert ok is True
    text = (tmp_path / "config").read_text()
    assert "Host jump\n\tHostName db.internal\n    UnknownCamelCase FooBar\n" in text
    assert text.rstrip().endswith("    User carol")  # new block appended last
    assert "# Global header - do not touch\n" in text
    assert "Match host *.internal\n    User matchuser\n" in text


def test_rename_replaces_only_the_target_block(tmp_path):
    cm = loaded_cm(tmp_path)
    conn = cm.find_connection_by_nickname("web")
    cm.update_ssh_config_file(
        conn,
        {"nickname": "web2", "hostname": "example.com", "username": "alice"},
        "web",
    )
    text = (tmp_path / "config").read_text()
    assert "Host web2\n" in text
    assert "Host web\n" not in text
    assert text.endswith(SUFFIX)


def test_repeated_blocks_for_same_host_collapse_on_edit(tmp_path):
    """Duplicate 'Host web' stanzas merge into one rewritten block on edit —
    mirrors ssh's merge semantics for repeated Host blocks."""
    doubled = ROOT + "\nHost web\n    Port 2222\n"
    cm = loaded_cm(tmp_path, doubled)
    conn = cm.find_connection_by_nickname("web")
    cm.update_ssh_config_file(
        conn, {"nickname": "web", "hostname": "example.com", "username": "bob"}, "web"
    )
    text = (tmp_path / "config").read_text()
    assert text.count("Host web\n") == 1


def test_edit_included_host_leaves_root_untouched(tmp_path):
    (tmp_path / "fragments").mkdir()
    frag = tmp_path / "fragments" / "extra"
    frag.write_text("Host frag\n    HostName frag.example.com\n    User alice\n")
    root_text = "Include fragments/extra\n\nHost web\n    HostName example.com\n"
    cm = loaded_cm(tmp_path, root_text)
    conn = cm.find_connection_by_nickname("frag")
    assert conn is not None and conn.source == str(frag)
    cm.update_ssh_config_file(
        conn,
        {"nickname": "frag", "hostname": "frag.example.com", "username": "bob",
         "source": str(frag)},
        "frag",
    )
    assert (tmp_path / "config").read_text() == root_text
    assert "    User bob\n" in frag.read_text()


# --- Surgical-merge guarantees (delivered by the document model) -----------


def test_comment_inside_edited_block_survives(tmp_path):
    text = (
        "Host web\n"
        "    # pinned to the old DC on purpose\n"
        "    HostName example.com\n"
        "    User alice\n"
    )
    cm = loaded_cm(tmp_path, text)
    conn = cm.find_connection_by_nickname("web")
    cm.update_ssh_config_file(
        conn, {"nickname": "web", "hostname": "example.com", "username": "bob"}, "web"
    )
    assert "# pinned to the old DC on purpose" in (tmp_path / "config").read_text()


def test_trailing_comment_after_edited_block_survives(tmp_path):
    text = (
        "Host web\n"
        "    HostName example.com\n"
        "\n"
        "# db cluster below\n"
        "Host db\n"
        "    HostName db.internal\n"
    )
    cm = loaded_cm(tmp_path, text)
    conn = cm.find_connection_by_nickname("web")
    cm.update_ssh_config_file(
        conn, {"nickname": "web", "hostname": "example.com", "username": "bob"}, "web"
    )
    assert "# db cluster below" in (tmp_path / "config").read_text()


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
    (tmp_path / "config").write_bytes(text.encode())
    cm = make_cm(tmp_path)
    cm.load_ssh_config()
    conn = cm.find_connection_by_nickname("web")
    assert conn is not None and conn.hostname == "example.com"

    cm.update_ssh_config_file(
        conn, {"nickname": "web", "hostname": "example.com", "username": "bob"}, "web"
    )
    raw = (tmp_path / "config").read_bytes().decode()
    assert "\n" not in raw.replace("\r\n", "")  # every line ending is CRLF
    assert "    User bob\r\n" in raw
    assert "Host db\r\n    HostName db.internal\r\n" in raw


def test_crlf_edit_preserving_comment_and_unknown_directive(tmp_path):
    """Preserved lines already carry CRLF; merging them with generated LF
    lines must not double-convert them to CR CR LF."""
    text = (
        "Host web\r\n"
        "    # keep\r\n"
        "    HostName example.com\r\n"
        "    SendEnv FOO\r\n"
    )
    (tmp_path / "config").write_bytes(text.encode())
    cm = make_cm(tmp_path)
    cm.load_ssh_config()
    conn = cm.find_connection_by_nickname("web")
    cm.update_ssh_config_file(
        conn,
        {"nickname": "web", "hostname": "example.com", "username": "bob",
         "extra_ssh_config": conn.extra_ssh_config},
        "web",
    )
    raw = (tmp_path / "config").read_bytes().decode()
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
    (tmp_path / "config").write_bytes(text.encode())
    cm = make_cm(tmp_path)
    cm.load_ssh_config()
    assert cm.remove_ssh_config_entry("web") is True
    assert (tmp_path / "config").read_bytes() == \
        b"Host db\r\n    HostName db.internal\r\n"


def test_missing_final_newline_preserved_when_other_block_edited(tmp_path):
    text = (
        "Host web\n"
        "    HostName example.com\n"
        "\n"
        "Host tail\n"
        "    HostName tail.example.com"  # no final newline
    )
    (tmp_path / "config").write_text(text)
    cm = make_cm(tmp_path)
    cm.load_ssh_config()
    conn = cm.find_connection_by_nickname("web")
    cm.update_ssh_config_file(
        conn, {"nickname": "web", "hostname": "example.com", "username": "bob"}, "web"
    )
    saved = (tmp_path / "config").read_text()
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
    cm = loaded_cm(tmp_path, text)
    conn = cm.find_connection_by_nickname("web")
    extras = conn.extra_ssh_config.lower()
    assert "sendenv foo" in extras and "sendenv bar" in extras
    assert "setenv a=1" in extras and "setenv b=2" in extras

    cm.update_ssh_config_file(
        conn,
        {"nickname": "web", "hostname": "example.com", "username": "u",
         "extra_ssh_config": conn.extra_ssh_config},
        "web",
    )
    saved = (tmp_path / "config").read_text()
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
    cm = loaded_cm(tmp_path, text)
    conn = cm.find_connection_by_nickname("web")
    cm.update_ssh_config_file(
        conn,
        {"nickname": "web", "hostname": "example.com", "username": "u",
         "extra_ssh_config": conn.extra_ssh_config},
        "web",
    )
    assert (tmp_path / "config").read_text().count("    SendEnv FOO\n") == 2


def test_unknown_directive_casing_survives_edit(tmp_path):
    text = (
        "Host web\n"
        "    HostName example.com\n"
        "    ServerAliveInterval 60\n"
    )
    cm = loaded_cm(tmp_path, text)
    conn = cm.find_connection_by_nickname("web")
    # A dialog-style payload carries the parsed extras back (lowercased today).
    cm.update_ssh_config_file(
        conn,
        {"nickname": "web", "hostname": "example.com", "username": "u",
         "extra_ssh_config": conn.extra_ssh_config},
        "web",
    )
    assert "ServerAliveInterval 60" in (tmp_path / "config").read_text()
