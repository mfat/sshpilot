"""
Tests that a parsed SSH config correctly populates the connection dialog and
that nothing is dropped when writing back.

Two layers:
  * Data-layer tests (always run): the parser fills the *structured* connection
    fields the dialog reads, and routes unaccounted-for directives into
    ``extra_ssh_config`` (the dialog's Advanced section) — and never duplicates
    a modelled directive there.
  * A real-libadwaita integration test (skipped where ``gi`` is stubbed by the
    suite) that drives the actual dialog: load populates the widgets + Advanced
    tab, and save returns the full set with nothing skipped.
"""

import asyncio

import pytest

asyncio.set_event_loop(asyncio.new_event_loop())

from sshpilot.connection_manager import ConnectionManager


def make_cm(path):
    cm = ConnectionManager.__new__(ConnectionManager)
    cm.connections = []
    cm.rules = []
    cm.ssh_config_path = str(path)
    return cm


RICH_HOST = """\
Host rich
    HostName rich.example.com
    User alice
    Port 2222
    IdentityFile ~/.ssh/id_ed25519
    IdentityFile ~/.ssh/work_rsa
    CertificateFile ~/.ssh/id_ed25519-cert.pub
    CertificateFile ~/.ssh/work_rsa-cert.pub
    IdentitiesOnly yes
    ForwardAgent yes
    ProxyJump bastion1,bastion2
    ForwardX11 yes
    LocalForward 8080 localhost:80
    IdentityAgent ~/.ssh/agent.sock
    AddKeysToAgent confirm
    PKCS11Provider /usr/lib/opensc-pkcs11.so
    SecurityKeyProvider /usr/lib/sk-libfido2.so
    Ciphers aes256-gcm@openssh.com
    Compression yes
    ServerAliveInterval 30
    SendEnv LANG LC_*
"""


class TestParserPopulatesDialogData:
    def _conn(self, tmp_path):
        cfg = tmp_path / "config"
        cfg.write_text(RICH_HOST)
        cm = make_cm(cfg)
        cm.load_ssh_config()
        return next(c for c in cm.connections if c.nickname == "rich")

    def test_structured_fields_populated(self, tmp_path):
        c = self._conn(tmp_path)
        assert c.hostname == "rich.example.com"
        assert c.username == "alice"
        assert c.port == 2222
        # multi-value lists
        assert len(c.identity_files) == 2
        assert any("id_ed25519" in f for f in c.identity_files)
        assert any("work_rsa" in f for f in c.identity_files)
        assert len(c.certificate_files) == 2
        # scalars / behaviours
        assert c.forward_agent is True
        assert c.proxy_jump == ["bastion1", "bastion2"]
        assert c.x11_forwarding is True
        assert any(r["type"] == "local" and r["listen_port"] == 8080 for r in c.forwarding_rules)
        assert c.identity_agent == "~/.ssh/agent.sock"
        assert c.add_keys_to_agent == "confirm"
        assert c.pkcs11_provider == "/usr/lib/opensc-pkcs11.so"
        assert c.security_key_provider == "/usr/lib/sk-libfido2.so"

    def test_unaccounted_directives_go_to_advanced(self, tmp_path):
        c = self._conn(tmp_path)
        extra = (c.extra_ssh_config or "").lower()
        # Directives the dialog has no dedicated widget for → Advanced section.
        assert "ciphers" in extra
        assert "compression" in extra
        assert "serveraliveinterval" in extra
        assert "sendenv" in extra

    def test_modelled_directives_not_duplicated_in_advanced(self, tmp_path):
        c = self._conn(tmp_path)
        extra = (c.extra_ssh_config or "").lower()
        for modelled in (
            "identityfile", "certificatefile", "hostname", "port ", "user ",
            "proxyjump", "forwardagent", "forwardx11", "localforward",
            "identitiesonly", "identityagent", "addkeystoagent",
            "pkcs11provider", "securitykeyprovider",
        ):
            assert modelled not in extra, f"{modelled!r} leaked into Advanced section"


class TestWriteNothingSkipped:
    def test_full_roundtrip_writes_everything(self, tmp_path):
        cm = make_cm(tmp_path / "config")
        data = {
            "nickname": "rich", "hostname": "rich.example.com", "username": "alice",
            "port": 2222, "auth_method": 0, "key_select_mode": 1,
            "identity_files": ["/h/.ssh/id_ed25519", "/h/.ssh/work_rsa", "/h/.ssh/third"],
            "certificate_files": ["/h/.ssh/a-cert.pub", "/h/.ssh/b-cert.pub"],
            "forward_agent": True,
            "proxy_jump": ["bastion1", "bastion2"],
            "x11_forwarding": True,
            "forwarding_rules": [
                {"type": "local", "listen_addr": "localhost", "listen_port": 8080,
                 "remote_host": "localhost", "remote_port": 80, "enabled": True},
                {"type": "remote", "listen_addr": "localhost", "listen_port": 2222,
                 "local_host": "localhost", "local_port": 22, "enabled": True},
                {"type": "dynamic", "listen_addr": "localhost", "listen_port": 1080,
                 "enabled": True},
            ],
            "identity_agent": "~/.ssh/agent.sock",
            "add_keys_to_agent": "confirm",
            "pkcs11_provider": "/usr/lib/opensc-pkcs11.so",
            "security_key_provider": "/usr/lib/sk-libfido2.so",
            "extra_ssh_config": "Ciphers aes256-gcm@openssh.com\nCompression yes",
        }
        entry = cm.format_ssh_config_entry(data)

        # multi-value: ALL identity files / certificates written (the old bug
        # wrote only the first IdentityFile).
        assert entry.count("IdentityFile ") == 3
        assert entry.count("CertificateFile ") == 2
        # everything else present
        assert "IdentitiesOnly yes" in entry
        assert "ForwardAgent yes" in entry
        assert "ProxyJump bastion1,bastion2" in entry
        assert "ForwardX11 yes" in entry
        assert "LocalForward" in entry and "RemoteForward" in entry and "DynamicForward" in entry
        assert "IdentityAgent ~/.ssh/agent.sock" in entry
        assert "AddKeysToAgent confirm" in entry
        assert "PKCS11Provider /usr/lib/opensc-pkcs11.so" in entry
        assert "SecurityKeyProvider /usr/lib/sk-libfido2.so" in entry
        assert "Ciphers aes256-gcm@openssh.com" in entry
        assert "Compression yes" in entry

    def test_reparse_preserves_all_identityfiles(self, tmp_path):
        """Write a 3-key host, re-parse it, and confirm all 3 survive."""
        cfg = tmp_path / "config"
        cfg.write_text(
            "Host k3\n    HostName k3.example.com\n"
            "    IdentityFile ~/.ssh/k1\n"
            "    IdentityFile ~/.ssh/k2\n"
            "    IdentityFile ~/.ssh/k3\n"
        )
        cm = make_cm(cfg)
        cm.load_ssh_config()
        c = next(x for x in cm.connections if x.nickname == "k3")
        assert len(c.identity_files) == 3
        entry = cm.format_ssh_config_entry({
            "nickname": "k3", "hostname": "k3.example.com", "auth_method": 0,
            "key_select_mode": 2, "identity_files": c.identity_files,
        })
        assert entry.count("IdentityFile ") == 3


# ---------------------------------------------------------------------------
# Real-libadwaita integration (skipped under the suite's stubbed gi)
# ---------------------------------------------------------------------------

def _real_gtk_available():
    try:
        import gi
        gi.require_version("Gtk", "4.0")
        gi.require_version("Adw", "1")
        from gi.repository import Adw
        Adw.init()
        return type(Adw.PreferencesGroup()).__name__ == "PreferencesGroup"
    except Exception:
        return False


@pytest.mark.integration
@pytest.mark.skipif(not _real_gtk_available(), reason="needs real libadwaita (gi is stubbed under the suite)")
def test_dialog_load_save_round_trip(tmp_path):
    from types import SimpleNamespace
    import gi
    from gi.repository import Gtk
    from sshpilot.connection_dialog import ConnectionDialog

    class CM:
        connections = []
        isolated_mode = False
        def load_ssh_keys(self): return []
        def get_key_passphrase(self, p): return ""
        def find_connection_by_nickname(self, n): return None
        def get_password(self, h, u): return None
        def format_ssh_config_entry(self, data): return ""

    dlg = ConnectionDialog(Gtk.Window(), connection=None, connection_manager=CM())
    conn = SimpleNamespace(
        nickname="rich", hostname="rich.example.com", username="alice", port=2222,
        auth_method=0, key_select_mode=1, pubkey_auth_no=False,
        identity_files=["/h/.ssh/id_ed25519", "/h/.ssh/work_rsa"], keyfile="/h/.ssh/id_ed25519",
        private_key=None,
        certificate_files=["/h/.ssh/a-cert.pub"], certificate="/h/.ssh/a-cert.pub",
        proxy_jump=["bastion1", "bastion2"], forward_agent=True, x11_forwarding=True,
        password="", key_passphrase="",
        extra_ssh_config="Ciphers aes256-gcm@openssh.com\nCompression yes",
        data={}, aliases=[], local_command="", remote_command="", pre_command="",
        forwarding_rules=[], identity_agent="~/.ssh/agent.sock", add_keys_to_agent="confirm",
        pkcs11_provider="", security_key_provider="",
    )
    dlg.connection = conn
    dlg.is_editing = True
    dlg.load_connection_data()

    # Widgets populated from the parsed connection.
    assert dlg.key_editor.get_paths() == ["/h/.ssh/id_ed25519", "/h/.ssh/work_rsa"]
    assert dlg.cert_editor.get_paths() == ["/h/.ssh/a-cert.pub"]
    advanced = dlg.advanced_tab.get_extra_ssh_config().lower()
    assert "ciphers" in advanced and "compression" in advanced

    captured = {}
    dlg.connect("connection-saved", lambda _d, payload: captured.update(payload))
    dlg.on_save_clicked()
    assert captured["identity_files"] == ["/h/.ssh/id_ed25519", "/h/.ssh/work_rsa"]
    assert captured["certificate_files"] == ["/h/.ssh/a-cert.pub"]
    assert captured["identity_agent"] == "~/.ssh/agent.sock"
    assert captured["add_keys_to_agent"] == "confirm"
    assert "ciphers" in captured["extra_ssh_config"].lower()
