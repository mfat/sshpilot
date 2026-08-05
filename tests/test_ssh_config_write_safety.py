"""
Protective guarantees for writing ~/.ssh/config.

SSH config is precious user data. ``SshConfigStore`` does an in-place,
block-targeted rewrite, and these tests pin the safety properties:

  * writes are atomic (temp file + os.replace) with a one-shot .bak backup, so a
    crash mid-write can never truncate the file;
  * everything we don't own is preserved verbatim — comments, Match blocks,
    other hosts, and per-host options the app doesn't model (extra_ssh_config);
  * the edited block is found and replaced regardless of separator form
    (``Host=name``), so we never leave a stale block and append a duplicate;
  * an Include directive following a block is never swallowed;
  * multi-value directives (IdentityFile) survive single-key dialog saves;
  * agent/hardware key directives parse into structured fields and round-trip.
"""

from sshpilot.core.connections.ssh_config_loader import load_ssh_configuration
from sshpilot.core.connections.ssh_config_store import SshConfigStore
from sshpilot.ssh_config_formatter import format_ssh_config_entry


def _store(tmp_path, text: str) -> SshConfigStore:
    path = tmp_path / "config"
    path.write_text(text, encoding="utf-8")
    return SshConfigStore(path)


class TestWriteSafety:
    def test_write_creates_backup_and_no_temp_leftovers(self, tmp_path):
        cfg = tmp_path / "config"
        cfg.write_text("Host edit\n    HostName old.example.com\n    Port 22\n")
        store = _store(tmp_path, cfg.read_text())
        store.update(
            "edit",
            {"nickname": "edit", "hostname": "new.example.com", "port": 2222,
             "auth_method": 0, "protocol": "ssh"},
            expected_generation=0,
        )
        assert (tmp_path / "config.bak").exists(), "a .bak backup must be created"
        assert (tmp_path / "config.bak").read_text().count("old.example.com") == 1
        # No temp scratch files left behind.
        leftovers = [p.name for p in tmp_path.iterdir() if p.name.startswith(".sshpilot-")]
        assert leftovers == [], f"temp files left behind: {leftovers}"

    def test_write_replaces_equals_form_host_without_duplicating(self, tmp_path):
        store = _store(tmp_path, "Host=eqhost\n    HostName old.example.com\n    Port 22\n")
        store.update(
            "eqhost",
            {"nickname": "eqhost", "hostname": "newer.example.com", "port": 2020,
             "auth_method": 0, "protocol": "ssh"},
            expected_generation=0,
        )
        out = (tmp_path / "config").read_text()
        # Exactly one Host block (the replacement), not a stale one + an appended dup.
        assert out.count("Host ") == 1, out
        assert "newer.example.com" in out and "old.example.com" not in out

    def test_write_does_not_swallow_following_include(self, tmp_path):
        store = _store(
            tmp_path,
            "Host edit\n"
            "    HostName old.example.com\n"
            "    Port 22\n"
            "Include ~/.ssh/extra.conf\n"
            "Host other\n"
            "    HostName other.example.com\n",
        )
        store.update(
            "edit",
            {"nickname": "edit", "hostname": "new.example.com", "port": 2222,
             "auth_method": 0, "protocol": "ssh"},
            expected_generation=0,
        )
        out = (tmp_path / "config").read_text()
        assert "Include ~/.ssh/extra.conf" in out, "Include after the block must survive"
        assert "Host other" in out

    def test_edit_changing_primary_key_keeps_extras(self, tmp_path):
        """Changing the primary key updates entry 1 and keeps the rest in order."""
        cfg = tmp_path / "config"
        cfg.write_text(
            "Host multi\n    HostName h.example.com\n"
            "    IdentityFile ~/.ssh/key_a\n"
            "    IdentityFile ~/.ssh/key_b\n"
        )
        store = _store(tmp_path, cfg.read_text())
        store.update(
            "multi",
            {
                "nickname": "multi", "hostname": "h.example.com", "port": 22,
                "auth_method": 0, "key_select_mode": 2,
                "keyfile": "/home/u/.ssh/key_x", "protocol": "ssh",
            },
            expected_generation=0,
        )
        out = cfg.read_text()
        ids = [ln.split(None, 1)[1].strip()
               for ln in out.splitlines() if ln.strip().lower().startswith("identityfile")]
        assert ids[0] == "/home/u/.ssh/key_x"
        assert any("key_b" in entry for entry in ids)
        assert not any("key_a" in entry for entry in ids)

    def test_single_key_edit_does_not_inject_list(self, tmp_path):
        """A host with one key: reconciliation is a no-op (no surprise list key)."""
        cfg = tmp_path / "config"
        cfg.write_text("Host one\n    HostName h.example.com\n    IdentityFile ~/.ssh/only\n")
        store = _store(tmp_path, cfg.read_text())
        store.update(
            "one",
            {"nickname": "one", "keyfile": "/home/u/.ssh/only", "auth_method": 0,
             "key_select_mode": 2, "protocol": "ssh"},
            expected_generation=0,
        )
        out = cfg.read_text()
        ids = [ln for ln in out.splitlines() if ln.strip().lower().startswith("identityfile")]
        assert len(ids) == 1

    def test_empty_username_omits_user_line(self):
        """A dialog payload with an empty username must not emit a User line."""
        with_user = format_ssh_config_entry(
            {"nickname": "x", "hostname": "example.com", "username": "alice"}
        )
        assert "    User alice" in with_user
        no_user = format_ssh_config_entry(
            {"nickname": "x", "hostname": "example.com", "username": ""}
        )
        assert "User" not in no_user

    def test_edit_preserves_unsurfaced_directives(self, tmp_path):
        """A dialog-style save payload omits ProxyCommand / standalone
        RequestTTY / ForwardAgent targets entirely; editing must not delete
        them from disk."""
        store = _store(
            tmp_path,
            "Host bast\n"
            "    HostName example.com\n"
            "    User alice\n"
            "    ProxyCommand ssh -W %h:%p jumphost\n"
            "    RequestTTY yes\n"
            "    ForwardAgent $SSH_AUTH_SOCK\n",
        )
        store.update(
            "bast",
            {
                "nickname": "bast",
                "hostname": "example.com",
                "username": "bob",
                "forward_agent": True,
                "auth_method": 0,
                "protocol": "ssh",
            },
            expected_generation=0,
        )
        text = (tmp_path / "config").read_text()
        assert "ProxyCommand ssh -W %h:%p jumphost" in text
        assert "RequestTTY yes" in text
        assert "ForwardAgent $SSH_AUTH_SOCK" in text
        assert "User bob" in text

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
        loaded = load_ssh_configuration(cfg, isolated=False)
        data = loaded.connections[0].data
        assert data["identity_agent"] == "~/.ssh/agent.sock"
        assert data["add_keys_to_agent"] == "confirm"
        assert data["pkcs11_provider"] == "/usr/lib/opensc-pkcs11.so"
        assert data["security_key_provider"] == "/usr/lib/sk-libfido2.so"
        # Not duplicated into the raw extra-config bucket.
        assert "pkcs11" not in (data.get("extra_ssh_config") or "").lower()
        assert "identityagent" not in (data.get("extra_ssh_config") or "").lower()

        entry = format_ssh_config_entry({
            "nickname": "hw", "hostname": "hw.example.com", "auth_method": 0,
            "key_select_mode": 0,
            "identity_agent": data["identity_agent"], "add_keys_to_agent": data["add_keys_to_agent"],
            "pkcs11_provider": data["pkcs11_provider"], "security_key_provider": data["security_key_provider"],
        })
        assert "IdentityAgent ~/.ssh/agent.sock" in entry
        assert "AddKeysToAgent confirm" in entry
        assert "PKCS11Provider /usr/lib/opensc-pkcs11.so" in entry
        assert "SecurityKeyProvider /usr/lib/sk-libfido2.so" in entry
