"""
Protective guarantees for writing ~/.ssh/config.

SSH config is precious user data. update_ssh_config_file() does an in-place,
block-targeted rewrite, and these tests pin the safety properties:

  * writes are atomic (temp file + os.replace) with a one-shot .bak backup, so a
    crash mid-write can never truncate the file;
  * everything we don't own is preserved verbatim — comments, Match blocks,
    other hosts, and per-host options the app doesn't model (extra_ssh_config);
  * the edited block is found and replaced regardless of separator form
    (``Host=name``), so we never leave a stale block and append a duplicate;
  * an Include directive following a block is never swallowed;
  * a single-argument (SOCKS) RemoteForward round-trips to valid syntax.
"""

import asyncio
from types import SimpleNamespace

import pytest

asyncio.set_event_loop(asyncio.new_event_loop())

from sshpilot.connection_manager import ConnectionManager


def make_cm(path):
    cm = ConnectionManager.__new__(ConnectionManager)
    cm.connections = []
    cm.rules = []
    cm.ssh_config_path = str(path)
    return cm


def _edit(cm, path, nickname, new_data):
    new_data = {**new_data, "nickname": nickname, "source": str(path)}
    conn = SimpleNamespace(source=str(path), nickname=nickname)
    cm.update_ssh_config_file(conn, new_data, original_nickname=nickname)
    return path.read_text()


class TestWriteSafety:
    def test_write_creates_backup_and_no_temp_leftovers(self, tmp_path):
        cfg = tmp_path / "config"
        cfg.write_text("Host edit\n    HostName old.example.com\n    Port 22\n")
        cm = make_cm(cfg)
        _edit(cm, cfg, "edit", {"hostname": "new.example.com", "port": 2222, "auth_method": 0})
        assert (tmp_path / "config.bak").exists(), "a .bak backup must be created"
        assert (tmp_path / "config.bak").read_text().count("old.example.com") == 1
        # No temp scratch files left behind.
        leftovers = [p.name for p in tmp_path.iterdir() if p.name.startswith(".sshpilot-")]
        assert leftovers == [], f"temp files left behind: {leftovers}"

    def test_write_preserves_foreign_content(self, tmp_path):
        cfg = tmp_path / "config"
        cfg.write_text(
            "# top comment\n\n"
            "Match host *.prod\n"
            "    ForwardAgent no\n\n"
            "Host keepme\n"
            "    HostName keep.example.com\n"
            "    Ciphers aes256-gcm@openssh.com\n\n"   # option the app doesn't model
            "Host edit\n"
            "    HostName old.example.com\n"
            "    Port 22\n"
        )
        cm = make_cm(cfg)
        out = _edit(cm, cfg, "edit", {"hostname": "new.example.com", "port": 2222, "auth_method": 0})
        assert "# top comment" in out
        assert "Match host *.prod" in out and "ForwardAgent no" in out
        assert "Host keepme" in out and "Ciphers aes256-gcm@openssh.com" in out
        assert "new.example.com" in out and "old.example.com" not in out

    def test_write_replaces_equals_form_host_without_duplicating(self, tmp_path):
        cfg = tmp_path / "config"
        cfg.write_text("Host=eqhost\n    HostName old.example.com\n    Port 22\n")
        cm = make_cm(cfg)
        out = _edit(cm, cfg, "eqhost", {"hostname": "newer.example.com", "port": 2020, "auth_method": 0})
        # Exactly one Host block (the replacement), not a stale one + an appended dup.
        assert out.count("Host ") == 1, out
        assert "newer.example.com" in out and "old.example.com" not in out

    def test_write_does_not_swallow_following_include(self, tmp_path):
        cfg = tmp_path / "config"
        cfg.write_text(
            "Host edit\n"
            "    HostName old.example.com\n"
            "    Port 22\n"
            "Include ~/.ssh/extra.conf\n"
            "Host other\n"
            "    HostName other.example.com\n"
        )
        cm = make_cm(cfg)
        out = _edit(cm, cfg, "edit", {"hostname": "new.example.com", "port": 2222, "auth_method": 0})
        assert "Include ~/.ssh/extra.conf" in out, "Include after the block must survive"
        assert "Host other" in out

    def test_socks_remoteforward_round_trips_to_valid_syntax(self, tmp_path):
        cm = make_cm(tmp_path / "config")
        entry = cm.format_ssh_config_entry({
            "nickname": "s", "hostname": "s.example.com", "auth_method": 0,
            "forwarding_rules": [{
                "type": "remote", "listen_addr": "localhost",
                "listen_port": 9999, "socks": True,
            }],
        })
        rf = [l.strip() for l in entry.splitlines() if "RemoteForward" in l]
        assert rf == ["RemoteForward localhost:9999"], rf

    def test_edit_save_preserves_all_identityfiles(self, tmp_path):
        """
        Regression: a host with multiple IdentityFile entries, edited via the
        single-key dialog (payload carries only the primary 'keyfile', no
        identity_files), must keep ALL keys on save — not collapse to one.
        Exercises the read -> reconcile -> atomic-write round trip.
        """
        cfg = tmp_path / "config"
        cfg.write_text(
            "Host multi\n"
            "    HostName old.example.com\n"
            "    IdentityFile ~/.ssh/key_a\n"
            "    IdentityFile ~/.ssh/key_b\n"
            "    IdentityFile ~/.ssh/key_c\n"
        )
        cm = make_cm(cfg)
        cm.load_ssh_config()
        conn = next(c for c in cm.connections if c.nickname == "multi")
        assert len(conn.identity_files) == 3

        # Dialog-style payload: changed hostname, single primary key, NO list.
        new_data = {
            "nickname": "multi", "hostname": "new.example.com", "port": 22,
            "auth_method": 0, "key_select_mode": 2,
            "keyfile": conn.identity_files[0], "source": str(cfg),
        }
        cm._preserve_multivalue_on_update(conn, new_data)
        cm.update_ssh_config_file(conn, new_data, original_nickname="multi")

        out = cfg.read_text()
        ids = [l.split(None, 1)[1].strip()
               for l in out.splitlines() if l.strip().lower().startswith("identityfile")]
        assert len(ids) == 3, f"all three keys must survive, got {ids}"
        assert any("key_a" in i for i in ids)
        assert any("key_b" in i for i in ids)
        assert any("key_c" in i for i in ids)
        assert "new.example.com" in out

    def test_edit_changing_primary_key_keeps_extras(self, tmp_path):
        """Changing the primary key updates entry 1 and keeps the rest in order."""
        cfg = tmp_path / "config"
        cfg.write_text(
            "Host multi\n    HostName h.example.com\n"
            "    IdentityFile ~/.ssh/key_a\n"
            "    IdentityFile ~/.ssh/key_b\n"
        )
        cm = make_cm(cfg)
        cm.load_ssh_config()
        conn = next(c for c in cm.connections if c.nickname == "multi")
        new_data = {
            "nickname": "multi", "hostname": "h.example.com", "port": 22,
            "auth_method": 0, "key_select_mode": 2,
            "keyfile": "/home/u/.ssh/key_x", "source": str(cfg),
        }
        cm._preserve_multivalue_on_update(conn, new_data)
        assert new_data["identity_files"][0] == "/home/u/.ssh/key_x"
        assert any("key_b" in f for f in new_data["identity_files"])
        assert not any("key_a" in f for f in new_data["identity_files"])

    def test_single_key_edit_does_not_inject_list(self, tmp_path):
        """A host with one key: reconciliation is a no-op (no surprise list key)."""
        cfg = tmp_path / "config"
        cfg.write_text("Host one\n    HostName h.example.com\n    IdentityFile ~/.ssh/only\n")
        cm = make_cm(cfg)
        cm.load_ssh_config()
        conn = next(c for c in cm.connections if c.nickname == "one")
        new_data = {"nickname": "one", "keyfile": conn.keyfile, "auth_method": 0}
        cm._preserve_multivalue_on_update(conn, new_data)
        assert "identity_files" not in new_data

    def test_agent_and_hardware_directives_round_trip(self, tmp_path):
        """IdentityAgent / AddKeysToAgent / PKCS11Provider / SecurityKeyProvider
        parse into structured fields (not duplicated into extra_ssh_config) and
        write back verbatim."""
        cfg = tmp_path / "config"
        cfg.write_text(
            "Host hw\n"
            "    HostName hw.example.com\n"
            "    IdentityAgent ~/.ssh/agent.sock\n"
            "    AddKeysToAgent confirm\n"
            "    PKCS11Provider /usr/lib/opensc-pkcs11.so\n"
            "    SecurityKeyProvider /usr/lib/sk-libfido2.so\n"
        )
        cm = make_cm(cfg)
        cm.load_ssh_config()
        c = next(x for x in cm.connections if x.nickname == "hw")
        assert c.identity_agent == "~/.ssh/agent.sock"
        assert c.add_keys_to_agent == "confirm"
        assert c.pkcs11_provider == "/usr/lib/opensc-pkcs11.so"
        assert c.security_key_provider == "/usr/lib/sk-libfido2.so"
        # Not duplicated into the raw extra-config bucket.
        assert "pkcs11" not in (c.extra_ssh_config or "").lower()
        assert "identityagent" not in (c.extra_ssh_config or "").lower()

        entry = cm.format_ssh_config_entry({
            "nickname": "hw", "hostname": "hw.example.com", "auth_method": 0,
            "key_select_mode": 0,
            "identity_agent": c.identity_agent, "add_keys_to_agent": c.add_keys_to_agent,
            "pkcs11_provider": c.pkcs11_provider, "security_key_provider": c.security_key_provider,
        })
        assert "IdentityAgent ~/.ssh/agent.sock" in entry
        assert "AddKeysToAgent confirm" in entry
        assert "PKCS11Provider /usr/lib/opensc-pkcs11.so" in entry
        assert "SecurityKeyProvider /usr/lib/sk-libfido2.so" in entry

    def test_atomic_write_leaves_original_intact_on_failure(self, tmp_path):
        """If serialising the new contents fails, the original file is untouched."""
        cfg = tmp_path / "config"
        original = "Host keep\n    HostName keep.example.com\n"
        cfg.write_text(original)
        cm = make_cm(cfg)
        # A non-string payload makes the temp-file write raise; the original must
        # remain because os.replace never runs.
        with pytest.raises(Exception):
            cm._safe_write_config(str(cfg), 12345)  # type: ignore[arg-type]
        assert cfg.read_text() == original
        leftovers = [p.name for p in tmp_path.iterdir() if p.name.startswith(".sshpilot-")]
        assert leftovers == [], f"temp files left behind: {leftovers}"


class TestManagedIdentityAgentBlock:
    """The global `Host *` IdentityAgent block written by apply_global_identity_agent()."""

    def test_add_update_remove_idempotent_preserve(self, tmp_path):
        cfg = tmp_path / "config"
        cfg.write_text(
            "Host myhost\n    HostName example.com\n\n"
            "# user comment\nHost *\n    ServerAliveInterval 30\n"
        )
        cm = make_cm(cfg)

        assert cm.apply_global_identity_agent("~/.1password/agent.sock") is True
        t = cfg.read_text()
        assert t.count("IdentityAgent ~/.1password/agent.sock") == 1
        assert "sshpilot: identity defaults (managed)" in t

        cm.apply_global_identity_agent("~/.1password/agent.sock")   # idempotent
        assert cfg.read_text().count("IdentityAgent") == 1

        cm.apply_global_identity_agent("~/.other.sock")             # update
        t = cfg.read_text()
        assert t.count("IdentityAgent") == 1 and "~/.other.sock" in t

        cm.apply_global_identity_agent(None)                        # remove
        t = cfg.read_text()
        assert "IdentityAgent" not in t and "sshpilot: identity" not in t
        # foreign content (incl. the user's own Host *) survived every step
        assert "ServerAliveInterval 30" in t and "Host myhost" in t and "user comment" in t

    def test_managed_block_goes_after_per_host_blocks(self, tmp_path):
        cfg = tmp_path / "config"
        cfg.write_text("Host a\n    HostName a.example\n")
        cm = make_cm(cfg)
        cm.apply_global_identity_agent("~/x.sock")
        t = cfg.read_text()
        assert t.index("Host a") < t.index("Host *")   # per-host wins first-match

    def test_remove_when_absent_does_not_write(self, tmp_path):
        cfg = tmp_path / "config"
        cfg.write_text("Host a\n    HostName a.example\n")
        cm = make_cm(cfg)
        before = cfg.read_text()
        assert cm.apply_global_identity_agent(None) is True
        assert cfg.read_text() == before                      # byte-for-byte unchanged
        assert not (tmp_path / "config.bak").exists()          # no write occurred

    def test_remove_with_no_file_creates_nothing(self, tmp_path):
        cfg = tmp_path / "config"                              # does not exist
        cm = make_cm(cfg)
        assert cm.apply_global_identity_agent(None) is True
        assert not cfg.exists()
