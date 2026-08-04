"""Golden characterization of the legacy connection persistence stack.

These tests freeze the *externally observable* behavior of the current
``ConnectionManager`` / ``GroupManager`` / ``Config`` persistence so the M3
headless connection repository can be verified against them. They describe
compatibility requirements — assertions target public methods and on-disk
results, not implementation internals.

The behavior recorded here must continue to hold after M3:

* SSH ``Host`` alias is the saved connection identity; no UUID survives.
* ``~/.ssh/config`` (plus includes) is the SSH source of truth.
* Non-SSH connections, groups, ordering, and metadata live in the app config.
* Writes are transactional: failed writes leave memory and disk unchanged.
"""

import asyncio
import os
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

asyncio.set_event_loop(asyncio.new_event_loop())

from sshpilot.connection_manager import Connection, ConnectionManager  # noqa: E402

FIXTURE_TREE = Path(__file__).parent / "fixtures" / "ssh_config"


class FakeConfig:
    """In-memory stand-in for the app Config JSON store.

    Mirrors the Config metadata surface (connections_meta) so the JSON-state
    characterization exercises the same externally observable API the real
    Config exposes.
    """

    def __init__(self):
        self.settings = {}

    def get_setting(self, key, default=None):
        return self.settings.get(key, default)

    def set_setting(self, key, value):
        self.settings[key] = value

    def get_connection_meta(self, key):
        meta_all = self.get_setting('connections_meta', {})
        if isinstance(meta_all, dict):
            value = meta_all.get(str(key or ''), {})
            return value if isinstance(value, dict) else {}
        return {}

    def set_connection_meta(self, key, meta):
        meta_all = self.get_setting('connections_meta', {})
        if not isinstance(meta_all, dict):
            meta_all = {}
        meta_all[str(key or '')] = meta or {}
        self.set_setting('connections_meta', meta_all)

    def is_pinned(self, nickname):
        return bool(self.get_connection_meta(nickname).get('pinned', False))

    def get_pinned_nicknames(self):
        meta_all = self.get_setting('connections_meta', {})
        if not isinstance(meta_all, dict):
            return []
        return [
            k for k, v in meta_all.items()
            if isinstance(v, dict) and v.get('pinned')
        ]

    def get_connection_tags(self, nickname):
        tags = self.get_connection_meta(nickname).get('tags', [])
        if isinstance(tags, list):
            return [str(t).strip() for t in tags if str(t).strip()]
        return []

    def set_connection_tags(self, nickname, tags):
        meta = self.get_connection_meta(nickname)
        cleaned = [str(t).strip() for t in (tags or []) if str(t).strip()]
        if cleaned:
            meta['tags'] = cleaned
        else:
            meta.pop('tags', None)
        self.set_connection_meta(nickname, meta)


@pytest.fixture
def manager(tmp_path):
    """A ConnectionManager instance with real persistence paths and a fake config."""
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


def _copy_fixture_tree(tmp_path: Path) -> Path:
    """Copy the golden fixture tree into *tmp_path* and return its root."""
    root = tmp_path / "ssh_config"
    shutil.copytree(FIXTURE_TREE, root)
    (root / "__init__.py").unlink(missing_ok=True)
    return root


def _load(cm, text: str) -> None:
    """Write *text* to the manager's config path and load it."""
    Path(cm.ssh_config_path).write_text(text, encoding="utf-8")
    cm.load_ssh_config()


def _text(cm) -> str:
    return Path(cm.ssh_config_path).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Loading: golden behavior
# ---------------------------------------------------------------------------


def test_load_empty_root_config_yields_no_connections(manager):
    _load(manager, "")
    assert manager.connections == []
    assert manager.rules == []


def test_load_missing_root_config_creates_empty_file(manager, tmp_path):
    manager.ssh_config_path = str(tmp_path / "absent")
    manager.load_ssh_config()
    assert Path(manager.ssh_config_path).read_text(encoding="utf-8") == (
        "# SSH configuration file\n\n"
    )
    assert manager.connections == []


def test_root_level_host_star_is_a_rule_not_a_connection(manager):
    _load(manager, "Host *\n    User global\n    ServerAliveInterval 30\n")
    assert manager.connections == []
    assert len(manager.rules) == 1
    assert manager.rules[0]["host"] == "*"


def test_exact_host_alias_is_the_connection_id(manager):
    _load(manager, "Host web\n    HostName example.com\n    User alice\n")
    conn = manager.find_connection_by_nickname("web")
    assert conn is not None
    assert conn.id == "web"
    assert conn.nickname == "web"
    assert conn.host == "web"
    assert conn.hostname == "example.com"
    assert conn.username == "alice"
    # No UUID survives loading.
    assert "uuid" not in conn.data


def test_multiple_aliases_create_one_connection_per_token(manager):
    _load(manager, "Host db jump\n    HostName=db.internal\n    User dbuser\n")
    nicknames = sorted(c.nickname for c in manager.connections)
    assert nicknames == ["db", "jump"]
    db = manager.find_connection_by_nickname("db")
    assert db.hostname == "db.internal"
    assert db.host == "db"
    assert db.source == manager.ssh_config_path


def test_negated_and_wildcard_hosts_become_rules(manager):
    _load(
        manager,
        "Host *.wild\n    User wilduser\n\n"
        "Host !negated\n    User never\n\n"
        "Host real\n    HostName real.example.com\n",
    )
    assert sorted(c.nickname for c in manager.connections) == ["real"]
    rule_hosts = {r["host"] for r in manager.rules}
    assert rule_hosts == {"*.wild", "!negated"}


def test_match_blocks_become_rules(manager):
    _load(manager, "Match host *.internal\n    User matchuser\n")
    assert manager.connections == []
    assert len(manager.rules) == 1
    assert "Match host *.internal" in manager.rules[0]["raw"]


def test_comments_and_blank_lines_are_preserved_by_edits(manager):
    _load(
        manager,
        "# header comment\n\n"
        "Host web\n"
        "    # pinned comment\n"
        "    HostName example.com\n"
        "    User alice\n\n"
        "Host other\n"
        "    HostName o.example.com\n",
    )
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
    assert "# header comment" in text
    assert "# pinned comment" in text
    assert "HostName example.org" in text
    assert "Host other" in text


def test_crlf_input_loads_connections(manager):
    _load(manager, "Host crlf\r\n    HostName c.example.com\r\n")
    assert len(manager.connections) == 1
    assert manager.connections[0].nickname == "crlf"


def test_inline_and_nested_include_load_fragments_with_source(manager, tmp_path):
    root = _copy_fixture_tree(tmp_path)
    manager.ssh_config_path = str(root / "golden_root.conf")
    manager.load_ssh_config()

    nicknames = sorted(c.nickname for c in manager.connections)
    assert "web" in nicknames
    assert "alpha" in nicknames          # inline include fragment
    assert "beta" in nicknames           # nested include fragment
    alpha = manager.find_connection_by_nickname("alpha")
    assert alpha.source.endswith("fragments/alpha.conf")
    beta = manager.find_connection_by_nickname("beta")
    assert beta.source.endswith("fragments/nested/beta.conf")
    # Options after an inline Include still belong to the host.
    inline = manager.find_connection_by_nickname("inline")
    assert inline is not None
    assert inline.hostname == "inline.example.com"
    assert inline.username == "inline-user"


def test_quoted_values_are_unwrapped(manager):
    _load(
        manager,
        'Host "two words" plain\n    HostName spaced.example.com\n',
    )
    assert manager.find_connection_by_nickname("two words") is not None
    assert manager.find_connection_by_nickname("plain") is not None
    conn = manager.find_connection_by_nickname("two words")
    assert conn.hostname == "spaced.example.com"


def test_multiple_identity_and_certificate_files_accumulate(manager):
    _load(
        manager,
        "Host web\n"
        "    HostName example.com\n"
        "    IdentityFile ~/.ssh/id_ed25519\n"
        "    IdentityFile ~/.ssh/id_rsa\n"
        "    CertificateFile ~/.ssh/web-cert.pub\n",
    )
    conn = manager.find_connection_by_nickname("web")
    assert conn.identity_files == [
        os.path.expanduser("~/.ssh/id_ed25519"),
        os.path.expanduser("~/.ssh/id_rsa"),
    ]
    assert conn.keyfile == os.path.expanduser("~/.ssh/id_ed25519")
    assert conn.certificate_files == [os.path.expanduser("~/.ssh/web-cert.pub")]


def test_identityfile_none_suppresses_identity(manager):
    _load(
        manager,
        "Host n\n"
        "    HostName n.example\n"
        "    IdentityFile none\n"
        "    IdentityFile ~/.ssh/id_ed25519\n",
    )
    conn = manager.find_connection_by_nickname("n")
    assert conn.identity_file_none is True
    assert conn.identity_files == [os.path.expanduser("~/.ssh/id_ed25519")]


def test_forwarding_directives_parse_to_rules(manager):
    _load(
        manager,
        "Host fwd\n"
        "    HostName f.example.com\n"
        "    LocalForward 8080 localhost:80\n"
        "    LocalForward 9090 10.0.0.5:9090\n"
        "    RemoteForward 6000 localhost:5900\n"
        "    DynamicForward 1080\n",
    )
    conn = manager.find_connection_by_nickname("fwd")
    rules = conn.forwarding_rules
    assert len(rules) == 4
    locals_ = [r for r in rules if r["type"] == "local"]
    assert [r["listen_port"] for r in locals_] == [8080, 9090]
    assert locals_[0]["remote_host"] == "localhost"
    assert locals_[1]["remote_host"] == "10.0.0.5"
    assert any(r["type"] == "remote" and r["listen_port"] == 6000 for r in rules)
    assert any(r["type"] == "dynamic" and r["listen_port"] == 1080 for r in rules)


def test_proxy_and_command_directives(manager):
    _load(
        manager,
        "Host bast\n"
        "    HostName example.com\n"
        "    ProxyJump jumphost\n"
        "    ProxyCommand ssh -W %h:%p gateway\n"
        "    ForwardAgent yes\n"
        "    RequestTTY force\n"
        "    LocalCommand echo connected\n"
        "    RemoteCommand tmux attach\n",
    )
    conn = manager.find_connection_by_nickname("bast")
    assert conn.proxy_jump == ["jumphost"]
    assert conn.proxy_command == "ssh -W %h:%p gateway"
    assert conn.forward_agent is True
    assert conn.request_tty == "force"
    assert conn.local_command == "echo connected"
    assert conn.remote_command == "tmux attach"


def test_unrecognized_directives_kept_as_extra_config(manager):
    _load(
        manager,
        "Host web\n    HostName example.com\n    UnknownDirective hello world\n",
    )
    conn = manager.find_connection_by_nickname("web")
    assert "unknowndirective hello world" in conn.extra_ssh_config


# ---------------------------------------------------------------------------
# Mutations: golden behavior
# ---------------------------------------------------------------------------


def test_create_appends_managed_block(manager):
    _load(manager, "")
    result = manager.create_connection(
        {
            "nickname": "new",
            "hostname": "example.net",
            "username": "u",
            "port": 22,
            "protocol": "ssh",
        }
    )
    assert result is not None
    assert manager.find_connection_by_nickname("new") is not None
    assert "Host new" in _text(manager)
    assert "HostName example.net" in _text(manager)


def test_update_rewrites_only_the_target_block(manager):
    _load(
        manager,
        "# header\n"
        "Host web\n"
        "    # pinned comment\n"
        "    HostName example.com\n"
        "    User alice\n\n"
        "Host other\n"
        "    HostName o.example.com\n",
    )
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
    assert "# header" in text
    assert "# pinned comment" in text
    assert "HostName example.org" in text
    assert "Host other" in text
    assert "HostName o.example.com" in text


def test_rename_rewrites_the_host_alias(manager):
    _load(manager, "Host web\n    HostName example.com\n    User alice\n")
    conn = manager.find_connection_by_nickname("web")
    ok = manager.update_connection(
        conn,
        {
            "nickname": "web2",
            "hostname": "example.com",
            "username": "alice",
            "protocol": "ssh",
        },
    )
    assert ok is True
    text = _text(manager)
    assert "Host web2" in text
    assert "Host web\n" not in text


def test_duplicate_creates_a_new_block(manager, tmp_path):
    _load(
        manager,
        "Host web\n    HostName example.com\n    User alice\n",
    )
    conn = manager.find_connection_by_nickname("web")
    from sshpilot.groups import GroupManager

    gm = GroupManager(FakeConfig())
    dup = manager.duplicate_connection(conn, gm)
    assert dup is not None
    assert dup.nickname != "web"
    assert manager.find_connection_by_nickname(dup.nickname) is not None
    assert "HostName example.com" in _text(manager)


def test_delete_removes_the_host_block(manager):
    _load(
        manager,
        "# header\nHost web\n    HostName example.com\n    User alice\n\n"
        "Host other\n    HostName o.example.com\n",
    )
    conn = manager.find_connection_by_nickname("web")
    ok = manager.remove_connection(conn, reload_config=False)
    assert ok is True
    assert manager.find_connection_by_nickname("web") is None
    text = _text(manager)
    assert "Host web" not in text
    assert "Host other" in text


def test_delete_of_one_alias_keeps_the_block_for_others(manager):
    _load(manager, "Host db jump\n    HostName=db.internal\n    User dbuser\n")
    conn = manager.find_connection_by_nickname("jump")
    ok = manager.remove_connection(conn, reload_config=False)
    assert ok is True
    text = _text(manager)
    assert "Host db" in text
    assert "jump" not in text.splitlines()[0]


def test_split_one_alias_from_multi_host_block(manager):
    _load(manager, "Host db jump\n    HostName=db.internal\n    User dbuser\n")
    conn = manager.find_connection_by_nickname("jump")
    ok = manager.update_connection(
        conn,
        {
            "nickname": "jump2",
            "hostname": "jump.example",
            "username": "j",
            "protocol": "ssh",
            "__split_from_group": True,
            "__split_source": manager.ssh_config_path,
            "__split_original_nickname": "jump",
        },
    )
    assert ok is True
    text = _text(manager)
    assert "Host db" in text
    assert "Host jump2" in text
    assert "HostName jump.example" in text


def test_edit_of_included_fragment_writes_back_to_the_fragment(manager, tmp_path):
    root = _copy_fixture_tree(tmp_path)
    manager.ssh_config_path = str(root / "golden_root.conf")
    manager.load_ssh_config()
    alpha = manager.find_connection_by_nickname("alpha")
    ok = manager.update_connection(
        alpha,
        {
            "nickname": "alpha",
            "hostname": "alpha2.example.com",
            "username": "root",
            "protocol": "ssh",
        },
    )
    assert ok is True
    fragment = Path(alpha.source)
    assert fragment.read_text(encoding="utf-8").find("HostName alpha2.example.com") >= 0
    # The root config itself was not touched.
    assert "alpha2.example.com" not in _text(manager)


def test_edit_preserves_match_and_wildcard_blocks(manager):
    _load(
        manager,
        "Match host *.internal\n    User matchuser\n\n"
        "Host *.wild\n    User wilduser\n\n"
        "Host web\n    HostName example.com\n    User alice\n",
    )
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
    assert "Match host *.internal" in text
    assert "User matchuser" in text
    assert "Host *.wild" in text
    assert "User wilduser" in text


def test_edit_preserves_multi_value_directives(manager):
    _load(
        manager,
        "Host web\n"
        "    HostName example.com\n"
        "    IdentityFile ~/.ssh/id_ed25519\n"
        "    IdentityFile ~/.ssh/id_rsa\n",
    )
    conn = manager.find_connection_by_nickname("web")
    ok = manager.update_connection(
        conn,
        {
            "nickname": "web",
            "hostname": "example.com",
            "username": "alice",
            "protocol": "ssh",
            "keyfile": os.path.expanduser("~/.ssh/id_ed25519"),
            "key_select_mode": 1,
        },
    )
    assert ok is True
    text = _text(manager)
    assert text.count("IdentityFile") == 2
    assert ".ssh/id_rsa" in text or "~/.ssh/id_rsa" in text


def test_failed_write_leaves_memory_and_disk_unchanged(manager, monkeypatch):
    _load(manager, "Host web\n    HostName example.com\n    User alice\n")
    conn = manager.find_connection_by_nickname("web")
    before = _text(manager)

    def _boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(manager, "_safe_write_config", _boom)
    ok = manager.update_connection(
        conn,
        {
            "nickname": "web2",
            "hostname": "example.org",
            "username": "bob",
            "protocol": "ssh",
        },
    )
    assert ok is False
    assert manager.find_connection_by_nickname("web") is not None
    assert manager.find_connection_by_nickname("web2") is None
    assert _text(manager) == before


def test_generation_increments_only_after_successful_writes(manager, monkeypatch):
    _load(manager, "Host web\n    HostName example.com\n    User alice\n")
    conn = manager.find_connection_by_nickname("web")
    before_gen = conn.generation

    def _boom(*args, **kwargs):
        raise OSError("disk full")

    original_write = manager._safe_write_config
    monkeypatch.setattr(manager, "_safe_write_config", _boom)
    ok = manager.update_connection(
        conn,
        {
            "nickname": "web",
            "hostname": "example.org",
            "username": "bob",
            "protocol": "ssh",
        },
    )
    assert ok is False
    assert conn.generation == before_gen  # failed write: no bump

    manager._safe_write_config = original_write
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
    assert conn.generation == before_gen + 1  # successful write: bump


# ---------------------------------------------------------------------------
# JSON-backed state: golden behavior
# ---------------------------------------------------------------------------


def _json_gm(manager):
    from sshpilot.groups import GroupManager

    return GroupManager(manager.config)


def test_non_ssh_connections_persist_to_config_json(manager, tmp_path):
    _load(manager, "")
    conn = Connection(
        {"nickname": "tel", "protocol": "telnet", "host": "10.0.0.5", "port": 2323}
    )
    ok = manager.update_connection(
        conn,
        {
            "nickname": "tel",
            "protocol": "telnet",
            "host": "10.0.0.5",
            "port": 2323,
        },
    )
    assert ok is True
    stored = manager.config.get_setting("connections.non_ssh", [])
    assert isinstance(stored, list)
    assert any(d.get("nickname") == "tel" for d in stored)
    # SSH config untouched.
    assert "tel" not in _text(manager)


def test_groups_persist_nested_with_color_and_order(manager):
    gm = _json_gm(manager)
    parent = gm.create_group("Production")
    child = gm.create_group("Web", parent_id=parent)
    gm.set_group_color(child, "#ff0000")
    blob = manager.config.get_setting("connection_groups", {})
    assert blob["groups"][child]["parent_id"] == parent
    assert blob["groups"][child]["color"] == "#ff0000"


def test_multi_group_membership_and_root_ordering(manager):
    gm = _json_gm(manager)
    g1 = gm.create_group("G1")
    g2 = gm.create_group("G2")
    gm.move_connection("web", g1)
    gm.copy_connection_to_group("web", g2)
    gm.move_connection("other", None)
    assert set(gm.get_connection_groups("web")) == {g1, g2}
    assert gm.get_connection_group("other") is None
    assert "other" in gm.root_connections
    assert "web" not in gm.root_connections


def test_metadata_tags_pinning_and_wol(manager):
    cfg = manager.config
    # set_connection_meta replaces the whole dict (legacy Config semantics),
    # so compose the full metadata before writing.
    cfg.set_connection_meta(
        "web",
        {"pinned": True, "wol": {"mac": "aa:bb:cc:dd:ee:ff", "port": 9}},
    )
    cfg.set_connection_tags("web", ["prod", "web"])
    assert cfg.is_pinned("web") is True
    assert cfg.get_pinned_nicknames() == ["web"]
    assert cfg.get_connection_tags("web") == ["prod", "web"]
    assert cfg.get_connection_meta("web")["wol"]["mac"] == "aa:bb:cc:dd:ee:ff"


def test_rename_preserves_group_and_metadata_references(manager, tmp_path):
    _load(manager, "Host old\n    HostName example.com\n    User alice\n")
    conn = manager.find_connection_by_nickname("old")
    gm = _json_gm(manager)
    gid = gm.create_group("Prod")
    gm.move_connection("old", gid)
    manager.config.set_connection_meta("old", {"pinned": True})
    manager.config.set_connection_tags("old", ["prod"])

    ok = manager.update_connection(
        conn,
        {
            "nickname": "new",
            "hostname": "example.com",
            "username": "alice",
            "protocol": "ssh",
        },
    )
    assert ok is True
    # Groups: legacy manager only migrates on reload; the canonical service
    # must migrate both references. GroupManager still knows the old alias.
    assert gm.get_connection_group("old") == gid
    # Metadata is keyed by alias and is NOT auto-migrated by legacy code.
    assert manager.config.get_connection_meta("old").get("pinned") is True


def test_delete_removes_metadata_reference(manager, tmp_path):
    _load(manager, "Host web\n    HostName example.com\n    User alice\n")
    conn = manager.find_connection_by_nickname("web")
    manager.config.set_connection_meta("web", {"pinned": True})
    manager.config.set_connection_tags("web", ["prod"])
    ok = manager.remove_connection(conn, reload_config=False)
    assert ok is True
    assert manager.config.get_connection_meta("web") == {}
