"""
Tests that a parsed SSH config correctly populates the connection dialog and
that nothing is dropped when writing back.

Two layers:
  * Data-layer tests (always run): the daemon loader fills the *structured*
    connection fields the dialog reads (``SshConfigStore.load()`` records),
    and routes unaccounted-for directives into ``extra_ssh_config`` (the
    dialog's Advanced section) — and never duplicates a modelled directive
    there. Round-trip rendering is pinned against
    ``ssh_config_formatter.format_ssh_config_entry``.
  * A real-libadwaita integration test (skipped where ``gi`` is stubbed by the
    suite) that drives the actual dialog: load populates the widgets + Advanced
    tab, and save returns the full set with nothing skipped.
"""

import asyncio

import pytest

asyncio.set_event_loop(asyncio.new_event_loop())

from sshpilot.core.connections.ssh_config_store import SshConfigStore
from sshpilot.ssh_config_formatter import format_ssh_config_entry


def load_record(tmp_path):
    cfg = tmp_path / "config"
    cfg.write_text(RICH_HOST)
    store = SshConfigStore(cfg)
    config = store.load()
    return next(r for r in config.connections if r.id == "rich")


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
    def test_structured_fields_populated(self, tmp_path):
        d = load_record(tmp_path).data
        assert d["hostname"] == "rich.example.com"
        assert d["username"] == "alice"
        assert d["port"] == 2222
        # multi-value lists
        assert len(d["identity_files"]) == 2
        assert any("id_ed25519" in f for f in d["identity_files"])
        assert any("work_rsa" in f for f in d["identity_files"])
        assert len(d["certificate_files"]) == 2
        # scalars / behaviours
        assert d["forward_agent"] is True
        assert d["proxy_jump"] == ["bastion1", "bastion2"]
        assert d["x11_forwarding"] is True
        assert any(r["type"] == "local" and r["listen_port"] == 8080
                   for r in d["forwarding_rules"])
        assert d["identity_agent"] == "~/.ssh/agent.sock"
        assert d["add_keys_to_agent"] == "confirm"
        assert d["pkcs11_provider"] == "/usr/lib/opensc-pkcs11.so"
        assert d["security_key_provider"] == "/usr/lib/sk-libfido2.so"

    def test_unaccounted_directives_go_to_advanced(self, tmp_path):
        extra = (load_record(tmp_path).data.get("extra_ssh_config") or "").lower()
        # Directives the dialog has no dedicated widget for → Advanced section.
        assert "ciphers" in extra
        assert "compression" in extra
        assert "serveraliveinterval" in extra
        assert "sendenv" in extra

    def test_modelled_directives_not_duplicated_in_advanced(self, tmp_path):
        extra = (load_record(tmp_path).data.get("extra_ssh_config") or "").lower()
        for modelled in (
            "identityfile", "certificatefile", "hostname", "port ", "user ",
            "proxyjump", "proxycommand", "forwardagent", "forwardx11", "localforward",
            "identitiesonly", "identityagent", "addkeystoagent",
            "pkcs11provider", "securitykeyprovider",
        ):
            assert modelled not in extra, f"{modelled!r} leaked into Advanced section"


class TestWriteNothingSkipped:
    def test_full_roundtrip_writes_everything(self, tmp_path):
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
        entry = format_ssh_config_entry(data)

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

    def test_writes_nonstandard_certificate_names_and_quotes_spaces(self, tmp_path):
        key_one = tmp_path / "alpha key"
        key_two = tmp_path / "bravo"
        cert_one = tmp_path / "signed one.pub"
        cert_two = tmp_path / "not-a-key-cert-name.pub"
        data = {
            "nickname": "certs",
            "hostname": "certs.example.com",
            "username": "alice",
            "auth_method": 0,
            "key_select_mode": 2,
            "identity_files": [str(key_one), str(key_two)],
            "certificate_files": [str(cert_one), str(cert_two)],
        }

        entry = format_ssh_config_entry(data)

        assert f'IdentityFile "{key_one}"' in entry
        assert f'CertificateFile "{cert_one}"' in entry
        assert f"CertificateFile {cert_two}" in entry
        assert entry.count("CertificateFile ") == 2

    def test_legacy_single_certificate_falls_back_to_certificate_files(self, tmp_path):
        cert = tmp_path / "legacy custom name.pub"
        data = {
            "nickname": "legacy-cert",
            "hostname": "legacy.example.com",
            "auth_method": 0,
            "key_select_mode": 2,
            "keyfile": str(tmp_path / "id_ed25519"),
            "certificate": str(cert),
        }

        entry = format_ssh_config_entry(data)

        assert f'CertificateFile "{cert}"' in entry
        assert entry.count("CertificateFile ") == 1

    def test_reparse_preserves_all_identityfiles(self, tmp_path):
        """Write a 3-key host, re-parse it, and confirm all 3 survive."""
        cfg = tmp_path / "config"
        cfg.write_text(
            "Host k3\n    HostName k3.example.com\n"
            "    IdentityFile ~/.ssh/k1\n"
            "    IdentityFile ~/.ssh/k2\n"
            "    IdentityFile ~/.ssh/k3\n"
        )
        store = SshConfigStore(cfg)
        record = next(r for r in store.load().connections if r.id == "k3")
        assert len(record.data["identity_files"]) == 3
        entry = format_ssh_config_entry({
            "nickname": "k3", "hostname": "k3.example.com", "auth_method": 0,
            "key_select_mode": 2, "identity_files": record.data["identity_files"],
        })
        assert entry.count("IdentityFile ") == 3

    def test_update_preserves_extra_certificatefiles_when_dialog_sends_primary_only(self, tmp_path):
        """The store's update path folds the unedited certificate list forward
        when the save payload only carries the new primary certificate."""
        cfg = tmp_path / "config"
        cfg.write_text(
            "Host cert-edit\n    HostName cert-edit.example.com\n"
            "    IdentityFile /h/.ssh/id_ed25519\n"
            "    CertificateFile /h/.ssh/old-primary.pub\n"
            "    CertificateFile /h/.ssh/nonstandard-extra.pub\n"
        )
        store = SshConfigStore(cfg)
        store.load()
        store.update(
            "cert-edit",
            {
                "nickname": "cert-edit",
                "hostname": "cert-edit.example.com",
                "certificate": "/h/.ssh/new primary.pub",
                "keyfile": "/h/.ssh/id_ed25519",
            },
            expected_generation=0,
        )

        fresh = next(r for r in store.load().connections if r.id == "cert-edit")
        assert fresh.data["certificate_files"] == [
            "/h/.ssh/new primary.pub",
            "/h/.ssh/nonstandard-extra.pub",
        ]


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
        proxy_command="ssh -W %h:%p bastion",
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
    assert captured["proxy_command"] == "ssh -W %h:%p bastion"
    assert "ciphers" in captured["extra_ssh_config"].lower()


@pytest.mark.integration
@pytest.mark.skipif(not _real_gtk_available(), reason="needs real libadwaita (gi is stubbed under the suite)")
def test_dialog_registers_with_parents_application():
    """A routed prompt (e.g. a vault master-password unlock while this dialog
    is open, issue #1197) finds its parent through Gtk.Application.get_windows().
    A bare Adw.Window is absent from that list unless it is explicitly
    registered, so the dialog must associate itself with its parent's
    application on construction."""
    from gi.repository import Gio, Gtk
    from sshpilot.connection_dialog import ConnectionDialog

    class CM:
        connections = []
        isolated_mode = False
        def load_ssh_keys(self): return []
        def get_key_passphrase(self, p): return ""
        def find_connection_by_nickname(self, n): return None
        def get_password(self, h, u): return None
        def format_ssh_config_entry(self, data): return ""

    app = Gtk.Application(application_id="org.sshpilot.test.dialog_registration")
    app.register(Gio.Cancellable())
    parent = Gtk.Window(application=app)

    dlg = ConnectionDialog(parent, connection=None, connection_manager=CM())

    assert dlg.get_application() is app
    assert dlg in list(app.get_windows())
